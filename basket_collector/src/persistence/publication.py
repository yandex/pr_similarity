"""Immutable v3 artifacts with one atomic pointer for working and published revisions."""

import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, StrictStr, field_validator, model_validator

from basket_collector.export_validation import validate_export_accounting
from basket_collector.models.artifacts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    InputReceipt,
    LineageReceipt,
    SelectionSnapshot,
    StageName,
    StrictModel,
    TasksDocument,
)
from basket_collector.persistence.artifact_io import (
    StorageError,
    _fsync_directory,
    artifact_bytes,
    atomic_write,
    durable_directory,
    read_json,
    stage_sha256,
)

STAGE_FILES: MappingProxyType[StageName, str] = MappingProxyType(
    {
        'collect': 'collected.json',
        'filter': 'filtered.json',
        'generate': 'tasks.json',
        'export': 'basket.json',
    }
)
MODELS = MappingProxyType(
    {'collect': CollectDocument, 'filter': FilterDocument, 'generate': TasksDocument, 'export': BasketDocument}
)


class ObjectRef(StrictModel):
    """The digest of exact immutable bytes stored in the local object directory."""

    sha256: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')


class Manifest(StrictModel):
    """Separate working stage heads from the last coherent published basket bundle."""

    schema_version: Literal[3]
    parent: StrictStr | None = None
    heads: dict[StageName, ObjectRef] = Field(default_factory=dict)
    journals: dict[StageName, ObjectRef] = Field(default_factory=dict)
    published: dict[StageName, ObjectRef] = Field(default_factory=dict)
    selection: ObjectRef | None = None
    published_selection: ObjectRef | None = None

    @field_validator('schema_version', mode='before')
    @classmethod
    def check_version(cls, value: object) -> int:
        """Accept only an explicit v3 manifest."""
        if type(value) is not int or value != 3:
            raise ValueError('publication requires integer schema_version 3')
        return value

    @model_validator(mode='after')
    def check_manifest(self) -> 'Manifest':
        """Reject incomplete published bundles and unsafe ancestry references."""
        if self.parent is not None and re.fullmatch(r'[0-9a-f]{64}', self.parent) is None:
            raise ValueError('invalid manifest parent')
        if self.published and set(self.published) != set(STAGE_FILES):
            raise ValueError('published bundle must contain all stages')
        if set(self.journals) - set(self.heads):
            raise ValueError('operation journal has no stage head')
        if bool(self.published) != (self.published_selection is not None):
            raise ValueError('published bundle must retain its selection')
        if self.selection is not None and 'filter' not in self.heads:
            raise ValueError('selection has no filter head')
        return self


class PublicationStore:
    """Publish new immutable revisions while retaining every previous successful bundle."""

    def __init__(self, output: Path) -> None:
        """Bind storage paths without creating files or importing existing artifacts."""
        self.output = Path(output)
        self.objects = self.output / '.objects'
        self.revisions = self.output / '.revisions'
        self.current = self.output / '.current'

    def check_layout(self) -> None:
        """Reject legacy or unmanaged output before overwrite, writes or external work."""
        for directory in (self.objects, self.revisions):
            if directory.is_symlink():
                raise StorageError('artifact storage directories cannot be symlinks')
        for stage, name in STAGE_FILES.items():
            path = self.output / name
            target = self._alias_target(stage)
            if path.is_symlink():
                if path.readlink() != target:
                    raise StorageError(f'invalid artifact alias: {name}')
            elif path.exists():
                self._check_version(read_json(path))
                raise StorageError(f'unmanaged version 3 artifact: {name}; choose an empty output')
            work = self.output / '.work' / f'{stage}.json'
            if work.exists():
                self._check_version(read_json(work))
        if self.current.exists() and not self.current.is_symlink():
            raise StorageError('current revision must be an owned symbolic link')
        selection = self.output / 'selection.json'
        if selection.is_symlink():
            if selection.readlink() != Path('.current/selection.json'):
                raise StorageError('invalid selection alias')
        elif selection.exists():
            self._check_version(read_json(selection))
            raise StorageError('unmanaged selection artifact; choose an empty output')

    @staticmethod
    def _check_version(payload: dict[str, Any]) -> None:
        """Reject every unsupported artifact version without a migration path."""
        meta = payload.get('meta', payload)
        if not isinstance(meta, dict) or type(meta.get('schema_version')) is not int or meta['schema_version'] != 3:
            raise StorageError('collector requires version 3 artifacts; choose a new output')

    @staticmethod
    def _alias_target(stage: StageName) -> Path:
        """Keep the public basket path pinned to the published bundle, not working heads."""
        area = 'published' if stage == 'export' else 'working'
        return Path('.current') / area / STAGE_FILES[stage]

    def _manifest(self) -> Manifest:
        """Validate the authoritative pointer and exact manifest bytes."""
        if not self.current.is_symlink():
            return Manifest(schema_version=3)
        target = self.current.readlink()
        if re.fullmatch(r'\.revisions/[0-9a-f]{64}', target.as_posix()) is None:
            raise StorageError('invalid current revision target')
        path = self.output / target / 'manifest.json'
        try:
            if path.parent.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != target.name:
                raise StorageError('manifest digest mismatch')
            manifest = Manifest.model_validate(read_json(path))
            self._check_views(path.parent, manifest)
            return manifest
        except (OSError, ValueError) as error:
            raise StorageError(f'invalid revision manifest: {error}') from error

    def _check_views(self, revision: Path, manifest: Manifest) -> None:
        """Ensure public file views resolve to precisely the objects in the manifest."""
        for name, references in (('working', manifest.heads), ('published', manifest.published)):
            directory = revision / name
            if directory.is_symlink() or not directory.is_dir():
                raise StorageError('invalid revision view directory')
            if {path.name for path in directory.iterdir()} != {STAGE_FILES[stage] for stage in references}:
                raise StorageError('revision view differs from manifest population')
            for stage, reference in references.items():
                path = directory / STAGE_FILES[stage]
                expected = Path('../../../.objects') / (reference.sha256 + '.json')
                if not path.is_symlink() or path.readlink() != expected:
                    raise StorageError('revision view differs from manifest reference')
        selection = revision / 'selection.json'
        if manifest.selection is None:
            if selection.exists() or selection.is_symlink():
                raise StorageError('unexpected revision selection view')
        else:
            expected = Path('../../.objects') / (manifest.selection.sha256 + '.json')
            if not selection.is_symlink() or selection.readlink() != expected:
                raise StorageError('revision selection view differs from manifest')

    def _get(self, reference: ObjectRef) -> dict[str, Any]:
        """Verify immutable bytes before parsing their v3 payload."""
        path = self.objects / (reference.sha256 + '.json')
        try:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != reference.sha256:
                raise StorageError('immutable artifact digest mismatch')
            payload = read_json(path)
            self._check_version(payload)
            return payload
        except OSError as error:
            raise StorageError('immutable artifact is missing or unreadable') from error

    def _put(self, payload: dict[str, Any]) -> ObjectRef:
        """Store immutable bytes once, refusing an inconsistent existing object."""
        self._check_version(payload)
        digest = hashlib.sha256(artifact_bytes(payload)).hexdigest()
        reference = ObjectRef(sha256=digest)
        path = self.objects / (digest + '.json')
        if path.exists() or path.is_symlink():
            self._get(reference)
        else:
            atomic_write(path, payload)
        return reference

    def load(self, stage: StageName) -> dict[str, Any] | None:
        """Read a current stage through its checked manifest reference."""
        self.check_layout()
        manifest = self._manifest()
        reference = manifest.heads.get(stage)
        if reference is None:
            return None
        payload = self._get(reference)
        if payload.get('meta', {}).get('stage') != stage:
            raise StorageError('manifest stage reference disagrees with artifact')
        return payload

    def journal(self, stage: StageName) -> dict[str, Any]:
        """Recover operational attempt state without adding it to semantic stage hashes."""
        self.check_layout()
        reference = self._manifest().journals.get(stage)
        if reference is None:
            return {}
        payload = self._get(reference)
        if payload.get('stage') != stage or not isinstance(payload.get('state'), dict):
            raise StorageError('invalid stage operation journal')
        return payload['state']

    def selection(self) -> SelectionSnapshot | None:
        """Read the last accepted selection independently from a newer candidate-only filter pass."""
        self.check_layout()
        reference = self._manifest().selection
        return SelectionSnapshot.model_validate(self._get(reference)) if reference is not None else None

    def _check_bundle(self, heads: dict[StageName, ObjectRef]) -> None:
        """Require a complete, mutually consistent predecessor chain before publication."""
        if set(heads) != set(STAGE_FILES):
            raise StorageError('cannot publish a basket without all predecessor stages')
        documents = {stage: MODELS[stage].model_validate(self._get(heads[stage])) for stage in STAGE_FILES}
        lineage: list[LineageReceipt] = []
        repository = None
        producer = None
        for stage in STAGE_FILES:
            document = documents[stage]
            meta = document.meta
            expected = InputReceipt(stage=lineage[-1].stage, sha256=lineage[-1].sha256) if lineage else None
            if meta.status != 'completed' or meta.lineage != lineage or meta.input != expected:
                raise StorageError('published bundle has inconsistent stage lineage')
            if repository is not None and (meta.repository != repository or meta.producer != producer):
                raise StorageError('published bundle has inconsistent source or producer')
            repository, producer = meta.repository, meta.producer
            lineage.append(
                LineageReceipt(
                    stage=stage,
                    sha256=stage_sha256(document),
                    settings=meta.settings,
                    settings_hash=meta.settings_hash,
                    producer=meta.producer,
                )
            )

        basket, collected, filtered, tasks = (documents[stage] for stage in ('export', 'collect', 'filter', 'generate'))
        assert isinstance(basket, BasketDocument) and isinstance(collected, CollectDocument)
        assert isinstance(filtered, FilterDocument) and isinstance(tasks, TasksDocument)
        validate_export_accounting(basket, collected, filtered, tasks)

    def _view(self, destination: Path, references: dict[StageName, ObjectRef]) -> None:
        """Build checked links to immutable objects inside a new private revision."""
        destination.mkdir()
        for stage, reference in references.items():
            self._get(reference)
            (destination / STAGE_FILES[stage]).symlink_to(Path('../../../.objects') / (reference.sha256 + '.json'))
        _fsync_directory(destination)

    def _revision(self, manifest: Manifest) -> Path:
        """Prepare the complete next view before changing any public pointer."""
        payload = manifest.model_dump(mode='json')
        digest = hashlib.sha256(artifact_bytes(payload)).hexdigest()
        destination = self.revisions / digest
        if destination.exists():
            if (destination / 'manifest.json').read_bytes() != artifact_bytes(payload):
                raise StorageError('existing revision manifest digest mismatch')
            self._check_views(destination, manifest)
            return destination
        durable_directory(self.revisions)
        temporary = self.revisions / ('.' + uuid.uuid4().hex + '.tmp')
        temporary.mkdir()
        try:
            self._view(temporary / 'working', manifest.heads)
            self._view(temporary / 'published', manifest.published)
            if manifest.selection is not None:
                self._get(manifest.selection)
                (temporary / 'selection.json').symlink_to(
                    Path('../../.objects') / (manifest.selection.sha256 + '.json')
                )
            atomic_write(temporary / 'manifest.json', payload)
            os.replace(temporary, destination)
            _fsync_directory(self.revisions)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination

    def _aliases(self) -> None:
        """Create stable public paths once; publication moves only the current pointer."""
        for stage, name in STAGE_FILES.items():
            path = self.output / name
            if not path.is_symlink():
                path.symlink_to(self._alias_target(stage))
        selection = self.output / 'selection.json'
        if selection.is_symlink():
            if selection.readlink() != Path('.current/selection.json'):
                raise StorageError('invalid selection alias')
        elif selection.exists():
            raise StorageError('unmanaged selection artifact')
        else:
            selection.symlink_to('.current/selection.json')
        _fsync_directory(self.output)

    def _switch(self, target: Path) -> None:
        """Atomically switch the manifest and all views, preserving the old revision."""
        temporary = self.output / ('.current.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.symlink_to(target.relative_to(self.output))
            os.replace(temporary, self.current)
            _fsync_directory(self.output)
        finally:
            if temporary.is_symlink():
                temporary.unlink()

    def commit(
        self, document: CollectDocument | FilterDocument | TasksDocument | BasketDocument, state: dict[str, Any]
    ) -> None:
        """Publish a completed v3 stage and its operation journal under the output lock."""
        self.check_layout()
        document = type(document).model_validate(document.model_dump(mode='json'))
        if document.meta.status != 'completed':
            raise StorageError('publication requires a completed stage')
        manifest = self._manifest()
        if self.current.is_symlink():
            manifest.parent = self.current.readlink().name
        stage = document.meta.stage
        manifest.heads[stage] = self._put(document.model_dump(mode='json'))
        manifest.journals[stage] = self._put({'schema_version': 3, 'stage': stage, 'state': state})
        if isinstance(document, FilterDocument) and document.selection is not None:
            manifest.selection = self._put(document.selection.model_dump(mode='json'))
        if isinstance(document, BasketDocument):
            self._check_bundle(manifest.heads)
            manifest.published = dict(manifest.heads)
            manifest.published_selection = manifest.selection
        target = self._revision(manifest)
        self._aliases()
        self._switch(target)

    def _history(self) -> Iterator[Manifest]:
        """Traverse checked immutable ancestry once with cycle and symbolic-link protection."""
        self.check_layout()
        manifest = self._manifest()
        visited = {self.current.readlink().name} if self.current.is_symlink() else set()
        while True:
            yield manifest
            parent = manifest.parent
            if parent is None:
                return
            if parent in visited:
                raise StorageError('publication ancestry cycle')
            visited.add(parent)
            path = self.revisions / parent / 'manifest.json'
            try:
                if (
                    path.is_symlink()
                    or path.parent.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != parent
                ):
                    raise StorageError('ancestor manifest digest mismatch')
                manifest = Manifest.model_validate(read_json(path))
            except (OSError, ValueError) as error:
                raise StorageError('invalid ancestor revision') from error

    def history(self, stage: StageName) -> Iterator[dict[str, Any]]:
        """Yield each immutable stage revision once, newest first, with checked ancestry."""
        objects: set[str] = set()
        for manifest in self._history():
            reference = manifest.heads.get(stage)
            if reference is not None and reference.sha256 not in objects:
                objects.add(reference.sha256)
                yield self._get(reference)

    def lookup(self, stage: StageName, digest: str) -> dict[str, Any]:
        """Resolve a frozen predecessor without relying on a changed current stage head."""
        for payload in self.history(stage):
            if stage_sha256(payload) == digest:
                return payload
        raise StorageError('frozen predecessor revision is unavailable')

    def historical_state(self, stage: StageName, digest: str) -> dict[str, Any]:
        """Find the operational journal committed atomically with a historical stage result."""
        for manifest in self._history():
            reference = manifest.heads.get(stage)
            if reference is None or stage_sha256(self._get(reference)) != digest:
                continue
            journal = manifest.journals.get(stage)
            if journal is None:
                raise StorageError('historical stage has no operation journal')
            payload = self._get(journal)
            if payload.get('stage') != stage or not isinstance(payload.get('state'), dict):
                raise StorageError('invalid historical operation journal')
            return payload['state']
        raise StorageError('historical operation journal is unavailable')
