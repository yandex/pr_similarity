"""Global filter algebra, deterministic null-last sorting and diff bounds."""

from dataclasses import replace
from starlette.datastructures import QueryParams

from viewer.presentation import Query, file_diff, public_files
from viewer.service import ViewerService
from viewer.config import Config, Limits
from viewer.database import Database
from viewer.storage import MemoryStore
from viewer.archive import validate_archive
from helpers import archive_fixture


def row(name='agent',score=.5,date='2026-10-05T10:00:00Z',user='alice'):
    return {'display_name':name,'score':score,'created_at':date,'run_id':name,'user':user,'tags':['setup:x'], 'tag_groups':{'model':['A','B'],'effort':['high'],'setup':['setup:x']},'basket_hash':'hash','pinned':True}


def test_filter_or_inside_and_between_groups_and_exclusive_dates():
    q=Query(QueryParams('model=absent&model=B&effort=high&user=alice&q=SETUP:X&from=2026-10-05T09:00:00Z&to=2026-10-05T10:00:01Z&pinned=1'))
    assert q.accepts(row())
    assert not q.accepts(row(user='bob'))
    assert not q.accepts(row(date='2026-10-05T10:00:01Z'))
    assert not Query(QueryParams('effort=low')).accepts(row())


def test_numeric_unknown_last_and_chronological_dates():
    items=[row('unknown',None),row('low',.1),row('high',.9)]
    assert [r['run_id'] for r in Query(QueryParams('direction=asc')).sorted(items)]==['low','high','unknown']
    assert [r['run_id'] for r in Query(QueryParams()).sorted(items)]==['high','low','unknown']
    items=[row('fraction',date='2026-10-05T10:00:00.1Z'),row('whole',date='2026-10-05T10:00:00Z')]
    assert Query(QueryParams('sort=created_at&direction=asc')).sorted(items)[0]['run_id']=='whole'


def test_total_token_sort_uses_reported_total_and_keeps_nulls_last():
    items=[]
    for name,total,date in [('unknown',None,'2026-10-05T10:00:00Z'),
                            ('zero',0,'2026-10-05T10:00:00Z'),
                            ('small',10,'2026-10-05T10:00:00Z'),
                            ('late',30,'2026-10-05T10:00:01Z'),
                            ('b',30,'2026-10-05T10:00:00Z'),
                            ('a',30,'2026-10-05T10:00:00Z')]:
        item=row(name,date=date)
        item['tokens']={'total':{'amount':total},'input':{'amount':9999 if total is None else 1000-total}}
        items.append(item)
    assert [r['run_id'] for r in Query(QueryParams('sort=total_tokens&direction=asc')).sorted(items)]==['zero','small','a','b','late','unknown']
    assert [r['run_id'] for r in Query(QueryParams('sort=total_tokens&direction=desc')).sorted(items)]==['a','b','late','small','zero','unknown']


def test_diff_byte_limit_is_explicit_and_file_id_not_a_path(tmp_path):
    path=tmp_path/'run.tar.gz';report,_=archive_fixture(path)
    imported=validate_archive(path,tmp_path/'data',Limits())
    limits=replace(Limits(),diff_bytes=32)
    config=Config(runtime=tmp_path/'runtime',storage='mock',limits=limits)
    svc=ViewerService(config,MemoryStore(),Database(config.runtime))
    svc.publish(imported,'alice','key')
    task=report['tasks'][0];file_id=public_files(task)[0]['file_id']
    diff=file_diff(svc,report['run']['id'],task['id'],file_id)
    assert diff['human_diff']['truncated'] and diff['agent_diff']['truncated']
    assert len(diff['human_diff']['text'].encode())<=32
    assert file_id!=task['scoring']['files'][0]['path']
    svc.db.close()


def test_basket_generation_projection_and_wall_time_unknown():
    from viewer.presentation import basket_metadata, wall_time
    basket={'meta':{'name':'Basket','description':'Public description','lineage':[{'stage':'generate','settings':{'source':'issue'}}]}}
    assert basket_metadata(basket)['task_source']=={'source':'issue','context':None}
    basket['meta']['lineage']=[]
    assert basket_metadata(basket)['task_source']=={'source':None,'context':None}
    assert wall_time({'created_at':'2026-09-30T15:00:00Z','finished_at':None}) is None
    assert wall_time({'created_at':'2026-09-30T15:02:00Z','finished_at':'2026-09-30T15:00:00Z'}) is None
