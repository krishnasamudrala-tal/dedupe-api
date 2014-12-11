import dedupe
import os
import json
import time
from cStringIO import StringIO
from api.queue import queuefunc
from api.app_config import DB_CONN, DOWNLOAD_FOLDER
from api.models import DedupeSession, User, entity_map
from api.database import worker_session
from api.utils.helpers import preProcess, makeDataDict, clusterGen, \
    makeSampleDict, windowed_query, updateSessionStatus
from api.utils.db_functions import updateEntityMap, writeBlockingMap, \
    writeRawTable, initializeEntityMap, writeProcessedTable, writeCanonRep
from sqlalchemy import Table, MetaData, Column, String, func
from sqlalchemy.sql import label
from sqlalchemy.exc import NoSuchTableError
from itertools import groupby
from operator import itemgetter
from csvkit import convert
from csvkit.unicsv import UnicodeCSVDictReader, UnicodeCSVReader, \
    UnicodeCSVWriter
from os.path import join, dirname, abspath
from datetime import datetime
import cPickle
from uuid import uuid4
from api.app_config import TIME_ZONE

def drawSample(session_id):
    sess = worker_session.query(DedupeSession).get(session_id)
    field_defs = json.loads(sess.field_defs)
    fields = [f['field'] for f in field_defs]
    d = dedupe.Dedupe(field_defs)
    data_d = makeSampleDict(sess.id, fields=fields)
    if len(data_d) < 50001:
        sample_size = 5000
    else:
        sample_size = round(int(len(data_d) * 0.01), -3)
    d.sample(data_d, sample_size=sample_size, blocked_proportion=1)
    sess.sample = cPickle.dumps(d.data_sample)
    worker_session.add(sess)
    worker_session.commit()

@queuefunc
def initializeSession(session_id, filename):
    file_obj = open('/tmp/%s_raw.csv' % session_id, 'rb')
    kwargs = {
        'session_id':session_id,
        'filename': filename,
        'file_obj':file_obj
    }
    writeRawTable(**kwargs)
    updateSessionStatus(session_id)
    sess = worker_session.query(DedupeSession).get(session_id)
    engine = worker_session.bind
    metadata = MetaData()
    raw_table = Table('raw_{0}'.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    sess.record_count = worker_session.query(raw_table).count()
    print 'session initialized'
    os.remove('/tmp/%s_raw.csv' % session_id)

@queuefunc
def initializeModel(session_id):
    sess = worker_session.query(DedupeSession).get(session_id)
    while True:
        worker_session.refresh(sess, ['field_defs', 'sample'])
        if not sess.field_defs:
            time.sleep(3)
        else:
            fields = [f['field'] for f in json.loads(sess.field_defs)]
            initializeEntityMap(session_id, fields)
            drawSample(session_id)
            updateSessionStatus(session_id)
            print 'got sample'
            break
    return 'woo'

def trainDedupe(session_id):
    dd_session = worker_session.query(DedupeSession)\
        .get(session_id)
    data_sample = cPickle.loads(dd_session.sample)
    deduper = dedupe.Dedupe(json.loads(dd_session.field_defs), 
        data_sample=data_sample)
    training_data = StringIO(dd_session.training_data)
    deduper.readTraining(training_data)
    deduper.train()
    settings_file_obj = StringIO()
    deduper.writeSettings(settings_file_obj)
    dd_session.settings_file = settings_file_obj.getvalue()
    worker_session.add(dd_session)
    worker_session.commit()
    deduper.cleanupTraining()

def blockDedupe(session_id, 
                table_name=None, 
                entity_table_name=None, 
                canonical=False):

    if not table_name:
        table_name = 'processed_{0}'.format(session_id)
    if not entity_table_name:
        entity_table_name = 'entity_{0}'.format(session_id)
    dd_session = worker_session.query(DedupeSession)\
        .get(session_id)
    deduper = dedupe.StaticDedupe(StringIO(dd_session.settings_file))
    engine = worker_session.bind
    metadata = MetaData()
    proc_table = Table(table_name, metadata,
        autoload=True, autoload_with=engine)
    entity_table = Table(entity_table_name, metadata,
        autoload=True, autoload_with=engine)
    for field in deduper.blocker.tfidf_fields:
        fd = worker_session.query(proc_table.c.record_id, 
            getattr(proc_table.c, field))
        field_data = (row for row in fd.yield_per(50000))
        deduper.blocker.tfIdfBlock(field_data, field)
        del field_data
    """ 
    SELECT p.* <-- need the fields that we trained on at least
        FROM processed as p
        LEFT OUTER JOIN entity_map as e
           ON s.record_id = e.record_id
        WHERE e.target_record_id IS NULL
    """
    proc_records = worker_session.query(proc_table)\
        .outerjoin(entity_table, proc_table.c.record_id == entity_table.c.record_id)\
        .filter(entity_table.c.target_record_id == None)
    fields = proc_table.columns.keys()
    full_data = ((getattr(row, 'record_id'), dict(zip(fields, row))) \
        for row in proc_records.yield_per(50000))
    block_gen = deduper.blocker(full_data)
    writeBlockingMap(session_id, block_gen, canonical=canonical)

def clusterDedupe(session_id, canonical=False, threshold=0.75):
    dd_session = worker_session.query(DedupeSession)\
        .get(session_id)
    deduper = dedupe.StaticDedupe(StringIO(dd_session.settings_file))
    engine = worker_session.bind
    metadata = MetaData()
    sc_format = 'small_cov_{0}'
    proc_format = 'processed_{0}'
    if canonical:
        sc_format = 'small_cov_{0}_cr'
        proc_format = 'cr_{0}'
    small_cov = Table(sc_format.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    proc = Table(proc_format.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    trained_fields = [f['field'] for f in json.loads(dd_session.field_defs)]
    proc_cols = [getattr(proc.c, f) for f in trained_fields]
    cols = [c for c in small_cov.columns] + proc_cols
    rows = worker_session.query(*cols)\
        .join(proc, small_cov.c.record_id == proc.c.record_id)
    fields = [c.name for c in cols]
    clustered_dupes = []
    while not clustered_dupes:
        clustered_dupes = deduper.matchBlocks(
            clusterGen(windowed_query(rows, small_cov.c.block_id, 50000), fields), 
            threshold=threshold
        )
        threshold = threshold - 0.1
    return clustered_dupes

@queuefunc
def dedupeRaw(session_id, threshold=0.75):
    trainDedupe(session_id)
    blockDedupe(session_id)
    clustered_dupes = clusterDedupe(session_id)
    updateEntityMap(clustered_dupes, session_id)
    updateSessionStatus(session_id)
    engine = worker_session.bind
    metadata = MetaData()
    entity_table = Table('entity_{0}'.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    entity_count = worker_session.query(entity_table.c.entity_id.distinct()).count()
    review_count = worker_session.query(entity_table.c.entity_id.distinct())\
        .filter(entity_table.c.clustered == False)\
        .count()
    dd = worker_session.query(DedupeSession).get(session_id)
    dd.entity_count = entity_count
    dd.review_count = review_count
    worker_session.add(dd)
    worker_session.commit()
    return 'ok'

@queuefunc
def makeCanonTable(session_id):
    ''' 
    Once canonical forms are deduplicated, make a table to store them
    '''
    engine = worker_session.bind
    metadata = MetaData()
    tables = [
        'entity_{0}_cr',
        'processed_{0}_cr',
        'block_{0}_cr',
        'plural_block_{0}_cr',
        'covered_{0}_cr',
        'plural_key_{0}_cr',
        'small_cov_{0}_cr',
    ]
    for table in tables:
        try:
            data_table = Table(table.format(session_id), 
                metadata, autoload=True, autoload_with=engine)
            data_table.drop(engine)
        except NoSuchTableError:
            pass
    writeCanonRep(session_id, name_pattern='canon_{0}')
    return 'ok'

@queuefunc
def dedupeCanon(session_id):
    engine = worker_session.bind
    metadata = MetaData()
    writeCanonRep(session_id)
    writeProcessedTable(session_id, 
                        proc_table_format='processed_{0}_cr', 
                        raw_table_format='cr_{0}')
    entity_table_name = 'entity_{0}_cr'.format(session_id)
    entity_table = entity_map(entity_table_name, metadata, record_id_type=String)
    entity_table.create(bind=engine, checkfirst=True)
    blockDedupe(session_id, 
        table_name='processed_{0}_cr'.format(session_id), 
        entity_table_name='entity_{0}_cr'.format(session_id), 
        canonical=True)
    clustered_dupes = clusterDedupe(session_id, canonical=True)
    fname = '/tmp/clusters_{0}.csv'.format(session_id)
    with open(fname, 'wb') as f:
        writer = UnicodeCSVWriter(f)
        for ids, scores in clustered_dupes:
            new_ent = unicode(uuid4())
            writer.writerow([
                new_ent,
                ids[0],
                scores[0],
                None,
                False,
                False,
            ])
            for id, score in zip(ids[1:], scores):
                writer.writerow([
                    new_ent,
                    id,
                    score,
                    ids[0],
                    False,
                    False,
                ])
    with open(fname, 'rb') as f:
        conn = engine.raw_connection()
        cur = conn.cursor()
        cur.copy_expert(''' 
            COPY "entity_{0}_cr" (
                entity_id,
                record_id,
                confidence,
                target_record_id,
                clustered,
                checked_out
            ) 
            FROM STDIN CSV'''.format(session_id), f)
        conn.commit()
    os.remove(fname)
    updateSessionStatus(session_id)
    return 'ok'

@queuefunc
def matchUnmatched(session_id):
    dd_session = worker_session.query(DedupeSession).get(session_id)
    dd = makeDataDict(session_id, name_pattern='canon_{0}')
    field_defs = json.loads(dd_session.field_defs)
    d = dedupe.Gazetteer(field_defs)
    d.readTraining(StringIO(dd_session.training_data))
    d.train()
    d.index(dd)
    sel = ''' 
        SELECT p.* 
            FROM "processed_{0}" AS p
            LEFT JOIN "entity_{0}" AS e
                ON p.record_id = e.record_id
            WHERE e.entity_id IS NULL
    '''.format(session_id)
    engine = worker_session.bind
    messy_dd = {}
    with engine.begin() as c:
        messy = c.execute(sel)
        for row in messy:
            messy_dd[row.record_id] = {k:v for k,v in row.items()}
    clusters = d.match(messy_dd, n_matches=5)
    match_list = []
    for cluster in clusters:
        for matches, confidence in cluster:
            matches = list(matches)
            matches.extend([
                confidence,
                datetime.now().replace(tzinfo=TIME_ZONE)
            ])
            match_list.append(matches)
    ''' 
    record_id, entity_id, confidence, last_update
    '''
    return None

@queuefunc
def bulkMatchWorker(session_id, file_contents, field_map, filename):
    ftype = convert.guess_format(filename)
    s = StringIO(file_contents)
    result = {
        'status': 'ok',
        'result': '',
        'message': ''
    }
    try:
        converted = convert.convert(s, ftype)
    except UnicodeDecodeError:
        result['status'] = 'error'
        result['message'] = 'Problem decoding file'
        return result
    sess = worker_session.query(DedupeSession).get(session_id)
    model_fields = [f.get('field') for f in json.loads(sess.field_defs)]
    s = StringIO(converted)
    reader = UnicodeCSVDictReader(s)
    rows = []
    for row in reader:
        r = {k: row.get(k, '') for k in field_map.values() if k}
        e = {k: '' for k in model_fields}
        for k,v in field_map.items():
            e[k] = r.get(v, '')
        rows.append(e)
    # Need a thing that will make a data_d without a DB
    data_d = iterDataDict(rows)
    deduper = dedupe.StaticGazetteer(StringIO(sess.gaz_settings_file))
    trained_data_d = makeDataDict(session_id, worker=True)
    deduper.index(trained_data_d)
    linked = deduper.match(messy_data_d, threshold=0, n_matches=5)
    s.seek(0)
    reader = UnicodeCSVReader(s)
    raw_header = reader.next()
    raw_header.extend([f for f in field_map.keys()])
    raw_rows = list(reader)
    fname = '%s_%s' % (datetime.now().isoformat(), filename)
    fpath = join(DOWNLOAD_FOLDER, fname)
    with open(fpath, 'wb') as outp:
        writer = UnicodeCSVWriter(outp)
        writer.writerow(raw_header)
        for link in linked:
            for l in link:
                id_set, conf = l
                messy_id, trained_id = id_set
                messy_row = raw_rows[int(messy_id)]
                trained_row = trained_data_d[trained_id]
                for k in field_map.keys():
                    messy_row.append(trained_row.get(k))
                writer.writerow(messy_row)
    result['result'] = fname
    return result
