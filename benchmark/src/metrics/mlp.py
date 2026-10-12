"""Learned `mlp` similarity metric as a fixed two-layer model."""

import math
from collections.abc import Mapping
from types import MappingProxyType

from .codebleu import CODEBLEU_COMPONENTS

_STRING_COMPONENTS: tuple[str, ...] = (
    'token_precision',
    'token_recall',
    'lines_precision',
    'lines_recall',
    'mtoken_precision',
    'mtoken_recall',
)

_BLEND_CODE: tuple[float, ...] = (
    0.24799232268552643,
    0.06976283504149337,
    0.11164381412869928,
    0.00901187469557109,
    0.32827901139875254,
    0.12856918904401876,
    0.030163259791314987,
    0.008629332585417171,
    0.042135786443601074,
    0.0238125741856052,
)
_BLEND_STRUCT: tuple[float, ...] = (
    0.03311266539139919,
    0.06244671429818529,
    0.045129807262190694,
    0.7881401892246751,
    0.046160089356825876,
    0.025010534466723848,
)
_BLEND_DEFAULT: tuple[float, ...] = (
    0.17536123707905218,
    0.15625951842574812,
    0.14214795029308072,
    0.17865446371165616,
    0.19079810250702398,
    0.1567787279834388,
)

_SCORE_CODE: tuple[float, ...] = (
    -0.15163323343492197,
    0.16354874225176064,
    0.892743775923137,
    0.05392943564355591,
    0.33840062941945576,
    0.0841199921980281,
    0.1095865550828513,
    0.12941665651227588,
    0.003208555902819084,
)
_SCORE_STRUCT: tuple[float, ...] = (
    0.41361462890928574,
    -0.4126211681734934,
    0.7271338196467237,
    0.2336947763166871,
    -0.06827523253074902,
    0.13246172180385454,
    0.17032978009429456,
    0.005037837854404753,
    0.1551922719018164,
)
_SCORE_DEFAULT: tuple[float, ...] = (
    0.039562755160451726,
    0.07209325413425526,
    0.881564444144411,
    0.12461342591531908,
    0.2517630868224644,
    0.1291673280172616,
    -0.038450686805914715,
    0.29238045723401357,
    0.1830828730732033,
)

_SIGMOID_CODE: tuple[float, float] = (5.817996960286306, -7.5932713605805615)
_SIGMOID_STRUCT: tuple[float, float] = (7.00655959116445, -7.827493849936938)
_SIGMOID_DEFAULT: tuple[float, float] = (5.336333970129912, -8.740930979133157)

_BRANCH_WEIGHTS: Mapping[str, tuple[tuple[float, ...], tuple[float, ...], tuple[float, float]]] = MappingProxyType(
    {
        'code': (_BLEND_CODE, _SCORE_CODE, _SIGMOID_CODE),
        'struct': (_BLEND_STRUCT, _SCORE_STRUCT, _SIGMOID_STRUCT),
        'default': (_BLEND_DEFAULT, _SCORE_DEFAULT, _SIGMOID_DEFAULT),
    }
)


def _sigmoid(x: float) -> float:
    """Return the logistic sigmoid, overflow-safe."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _pair_components_from_features(
    string_features: dict[str, float],
    codebleu: dict[str, float] | None,
) -> list[float]:
    """Return the layer-1 component vector from precomputed string features."""
    string_vector = [string_features[name] for name in _STRING_COMPONENTS]
    if codebleu is None:
        return string_vector
    return [codebleu[name] for name in CODEBLEU_COMPONENTS] + string_vector


def _blend(components: list[float], weights: tuple[float, ...]) -> float:
    """Return the convex blend for one pair."""
    return sum(weight * value for weight, value in zip(weights, components))


def mlp_pair_similarity_from_features(
    branch: str,
    string_features: dict[str, float],
    codebleu: dict[str, float] | None = None,
) -> float:
    """Return layer-1 MLP pair similarity for one branch."""
    if branch == 'code' and codebleu is None:
        raise ValueError('code branch requires codebleu components')
    blend_weights, _, _ = _BRANCH_WEIGHTS[branch]
    return _blend(_pair_components_from_features(string_features, codebleu), blend_weights)


def mlp_similarity_from_pair_scores(
    branch: str,
    pair_scores: dict[str, float],
    diff_features: tuple[float, float, float, float, float, float],
) -> float:
    """Return calibrated layer-2 MLP similarity from precomputed pair scores."""
    _, score_weights, (cal_a, cal_b) = _BRANCH_WEIGHTS[branch]
    features = (
        pair_scores['base_human'],
        pair_scores['base_ai'],
        pair_scores['human_ai'],
        *diff_features,
    )
    direction = sum(weight * feature for weight, feature in zip(score_weights, features))
    return _sigmoid(cal_a * direction + cal_b)
