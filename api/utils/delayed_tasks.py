import dedupe
import os
import json
import time
from cStringIO import StringIO
from api.queue import queuefunc
from api.app_config import DB_CONN, DOWNLOAD_FOLDER
from api.models import DedupeSession, User, entity_map
from api.database import worker_session
from api.utils.helpers import preProcess, clusterGen, \
    makeSampleDict, windowed_query, getDistinct
from api.utils.db_functions import updateEntityMap, writeBlockingMap, \
    writeRawTable, initializeEntityMap, writeProcessedTable, writeCanonRep, \
    addRowHash
from api.utils.review_machine import ReviewMachine
from sqlalchemy import Table, MetaData, Column, String, func, text
from sqlalchemy.sql import label
from sqlalchemy.exc import NoSuchTableError, ProgrammingError
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

@queuefunc
def bulkMarkClusters(session_id, user=None):
    dd = worker_session.query(DedupeSession).get(session_id)
    engine = worker_session.bind
    now =  datetime.now().replace(tzinfo=TIME_ZONE)
    upd_vals = {
        'user_name': user, 
        'clustered': True,
        'match_type': 'bulk accepted',
        'last_update': now,
    }
    upd = text(''' 
        UPDATE "entity_{0}" SET 
            entity_id=subq.entity_id,
            clustered= :clustered,
            reviewer = :user_name,
            match_type = :match_type,
            last_update = :last_update
        FROM (
                SELECT 
                    s.entity_id AS entity_id,
                    e.record_id 
                FROM "entity_{0}" AS e
                JOIN (
                    SELECT 
                        record_id, 
                        entity_id
                    FROM "entity_{0}"
                ) AS s
                    ON e.target_record_id = s.record_id
            ) as subq 
        WHERE "entity_{0}".record_id=subq.record_id 
            AND ( "entity_{0}".clustered=FALSE 
                  OR "entity_{0}".match_type != 'clerical review' )
        RETURNING "entity_{0}".entity_id
        '''.format(session_id))
    with engine.begin() as c:
        child_entities = c.execute(upd, **upd_vals)
    upd = text(''' 
        UPDATE "entity_{0}" SET
            clustered = :clustered,
            reviewer = :user_name,
            last_update = :last_update,
            match_type = :match_type
        WHERE target_record_id IS NULL
            AND clustered=FALSE
        RETURNING entity_id;
    '''.format(session_id))
    with engine.begin() as c:
        parent_entities = c.execute(upd, **upd_vals)
    child_entities = set([c.entity_id for c in child_entities])
    parent_entities = set([p.entity_id for p in parent_entities])
    count = len(child_entities.union(parent_entities))
    with engine.begin() as conn:
        conn.execute(text('''
          UPDATE dedupe_session SET 
            review_count = 0,
            entity_count = :entity_count
          WHERE id = :id
          '''), entity_count=count, id=session_id)
    dedupeCanon(session_id)
    return None

@queuefunc
def bulkMarkCanonClusters(session_id, user=None):
    sess = worker_session.query(DedupeSession).get(session_id)
    engine = worker_session.bind
    upd_vals = {
        'user_name': user, 
        'clustered': True,
        'match_type': 'bulk accepted - canon',
        'last_update': datetime.now().replace(tzinfo=TIME_ZONE)
    }
    upd = text(''' 
        UPDATE "entity_{0}" SET 
            entity_id=subq.entity_id,
            clustered= :clustered,
            reviewer = :user_name,
            match_type = :match_type,
            last_update = :last_update
        FROM (
            SELECT 
                c.record_id as canon_record_id,
                c.entity_id, 
                e.record_id 
            FROM "entity_{0}" as e
            JOIN "entity_{0}_cr" as c 
                ON e.entity_id = c.record_id 
            LEFT JOIN (
                SELECT record_id, target_record_id FROM "entity_{0}"
                ) AS s 
                ON e.record_id = s.target_record_id
            ) as subq 
        WHERE "entity_{0}".record_id=subq.record_id
        RETURNING "entity_{0}".entity_id, subq.canon_record_id
        '''.format(session_id))
    with engine.begin() as c:
        updated = c.execute(upd,**upd_vals)
        for row in updated:
            c.execute(text(''' 
                    UPDATE "entity_{0}_cr" SET
                        target_record_id = :target,
                        clustered = TRUE
                    WHERE record_id = :record_id
                '''.format(session_id)),
                target=row[0], record_id=row[1])
    getMatchingReady(session_id)

@queuefunc
def getMatchingReady(session_id):
    addRowHash(session_id)
    cleanupTables(session_id)
    engine = worker_session.bind
    with engine.begin() as conn:
        conn.execute('DROP TABLE IF EXISTS "match_blocks_{0}"'\
            .format(session_id))
        conn.execute(''' 
            CREATE TABLE "match_blocks_{0}" (
                block_key VARCHAR, 
                record_id BIGINT
            )
            '''.format(session_id))
    sess = worker_session.query(DedupeSession).get(session_id)
    field_defs = json.loads(sess.field_defs)

    # Save Gazetteer settings
    d = dedupe.Gazetteer(field_defs)

    # Disabling canopy based predicates for now
    for definition in d.data_model.primary_fields:
        for idx, predicate in enumerate(definition.predicates):
            if predicate.type == 'TfidfPredicate':
                definition.predicates.pop(idx)

    d.readTraining(StringIO(sess.training_data))
    d.train()
    g_settings = StringIO()
    d.writeSettings(g_settings)
    g_settings.seek(0)
    sess.gaz_settings_file = g_settings.getvalue()
    worker_session.add(sess)
    worker_session.commit()

    # Write match_block table
    model_fields = list(set([f['field'] for f in field_defs]))
    fields = ', '.join(['p.{0}'.format(f) for f in model_fields])
    sel = ''' 
        SELECT 
          p.record_id, 
          {0}
        FROM "processed_{1}" AS p 
        LEFT JOIN "exact_match_{1}" AS e 
          ON p.record_id = e.match 
        WHERE e.record_id IS NULL;
        '''.format(fields, session_id)
    conn = engine.connect()
    rows = conn.execute(sel)
    data = ((getattr(row, 'record_id'), dict(zip(model_fields, row[1:]))) \
        for row in rows)
    block_gen = d.blocker(data)
    s = StringIO()
    writer = UnicodeCSVWriter(s)
    writer.writerows(block_gen)
    conn.close()
    s.seek(0)
    conn = engine.raw_connection()
    curs = conn.cursor()
    try:
        curs.copy_expert('COPY "match_blocks_{0}" FROM STDIN CSV'\
            .format(session_id), s)
        conn.commit()
    except Exception, e: # pragma: no cover
        conn.rollback()
        raise e
    conn.close()
    with engine.begin() as conn:
        conn.execute('''
            CREATE INDEX "match_blocks_key_{0}_idx" 
              ON "match_blocks_{0}" (block_key)
            '''.format(session_id)
        )

    # Get review count
    sel = ''' 
      SELECT COUNT(*)
      FROM "raw_{0}" AS p
      LEFT JOIN "entity_{0}" AS e
        ON p.record_id = e.record_id
      WHERE e.record_id IS NULL
    '''.format(session_id)
    count = list(engine.execute(sel))
    sess.status = 'matching ready'
    sess.review_count = count[0][0]
    worker_session.add(sess)
    worker_session.commit()
    return None

@queuefunc
def cleanupTables(session_id, tables=None):
    engine = worker_session.bind
    if not tables:
        tables = [
            'processed_{0}_cr',
            'block_{0}_cr',
            'plural_block_{0}_cr',
            'covered_{0}_cr',
            'plural_key_{0}_cr',
            'small_cov_{0}_cr',
            'cr_{0}',
            'block_{0}',
            'plural_block_{0}',
            'covered_{0}',
            'plural_key_{0}',
        ]
    conn = engine.connect()
    trans = conn.begin()
    for table in tables:
        tname = table.format(session_id)
        try:
            conn.execute('DROP TABLE "{0}"'.format(tname))
            trans.commit()
        except Exception, e:
            trans.rollback()
    conn.close()

def drawSample(session_id):
    sess = worker_session.query(DedupeSession).get(session_id)
    field_defs = json.loads(sess.field_defs)
    fields = list(set([f['field'] for f in field_defs]))
    d = dedupe.Dedupe(field_defs)
    data_d = makeSampleDict(sess.id, fields=fields)
    if len(data_d) < 50001:
        sample_size = 5000
    else: # pragma: no cover
        sample_size = round(int(len(data_d) * 0.01), -3)
    d.sample(data_d, sample_size=sample_size, blocked_proportion=1)
    sess.sample = cPickle.dumps(d.data_sample)
    worker_session.add(sess)
    worker_session.commit()

@queuefunc
def initializeSession(session_id):
    sess = worker_session.query(DedupeSession).get(session_id)
    file_path = '/tmp/{0}_raw.csv'.format(session_id)
    kwargs = {
        'session_id':session_id,
        'file_path':file_path
    }
    writeRawTable(**kwargs)
    engine = worker_session.bind
    metadata = MetaData()
    raw_table = Table('raw_{0}'.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    sess.record_count = worker_session.query(raw_table).count()
    worker_session.add(sess)
    worker_session.commit()
    print 'session initialized'

@queuefunc
def initializeModel(session_id):
    sess = worker_session.query(DedupeSession).get(session_id)
    while True:
        worker_session.refresh(sess, ['field_defs', 'sample'])
        if not sess.field_defs: # pragma: no cover
            time.sleep(3)
        else:
            field_defs = json.loads(sess.field_defs)
            fields = list(set([f['field'] for f in field_defs]))
            writeProcessedTable(session_id)
            updated_fds = []
            for field in field_defs:
                if field['type'] == 'Categorical':
                    categories = getDistinct(field['field'], session_id)
                    if len(categories) <= 6:
                        field.update({'categories': categories})
                    else:
                        field['type'] = 'Exact'
                updated_fds.append(field)
            sess.field_defs = json.dumps(updated_fds)
            worker_session.add(sess)
            worker_session.commit()
            initializeEntityMap(session_id, fields)
            drawSample(session_id)
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
        with engine.begin() as conn:
            fd = conn.execute('select record_id, {0} from "{1}"'.format(field, table_name))
            deduper.blocker.tfIdfBlock(fd, field)
    """ 
    SELECT p.* <-- need the fields that we trained on at least
        FROM processed as p
        LEFT OUTER JOIN entity_map as e
           ON p.record_id = e.record_id
        WHERE e.target_record_id IS NULL
    """
    proc_records = worker_session.query(proc_table)\
        .outerjoin(entity_table, proc_table.c.record_id == entity_table.c.record_id)\
        .filter(entity_table.c.target_record_id == None)
    fields = proc_table.columns.keys()
    full_data = ((getattr(row, 'record_id'), dict(zip(fields, row))) \
        for row in proc_records.yield_per(50000))
    return deduper.blocker(full_data)

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
    trained_fields = list(set([f['field'] for f in json.loads(dd_session.field_defs)]))
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
def reDedupeRaw(session_id, threshold=0.75):
    sess = worker_session.query(DedupeSession).get(session_id)
    field_defs = json.loads(sess.field_defs)
    fields = list(set([f['field'] for f in field_defs]))
    initializeEntityMap(session_id, fields)
    dedupeRaw(session_id, threshold=threshold)
    sess.status = 'entity map updated'
    worker_session.add(sess)
    worker_session.commit()
    return 'ok'

@queuefunc
def reDedupeCanon(session_id, threshold=0.25):
    upd = text(''' 
        UPDATE "entity_{0}" SET
            entity_id = subq.old_entity_id,
            last_update = :last_update
        FROM (
            SELECT 
               c.record_id AS old_entity_id,
               e.entity_id AS new_entity_id
            FROM "entity_{0}_cr" AS c
            JOIN "entity_{0}" AS e
                ON c.target_record_id = e.entity_id
            WHERE c.clustered = TRUE
            ) AS subq
        WHERE "entity_{0}".entity_id = subq.new_entity_id
    '''.format(session_id))
    engine = worker_session.bind
    last_update = datetime.now().replace(tzinfo=TIME_ZONE)
    with engine.begin() as c:
        c.execute(upd, last_update=last_update)
    dedupeCanon(session_id, threshold=threshold)
    sess = worker_session.query(DedupeSession).get(session_id)
    sess.status = 'canon clustered'
    worker_session.add(sess)
    worker_session.commit()
    return 'ok'

@queuefunc
def dedupeRaw(session_id, threshold=0.75):
    trainDedupe(session_id)
    block_gen = blockDedupe(session_id)
    writeBlockingMap(session_id, block_gen, canonical=False)
    clustered_dupes = clusterDedupe(session_id)
    updateEntityMap(clustered_dupes, session_id)
    engine = worker_session.bind
    metadata = MetaData()
    entity_table = Table('entity_{0}'.format(session_id), metadata,
        autoload=True, autoload_with=engine, keep_existing=True)
    entity_count = worker_session.query(entity_table.c.entity_id.distinct())\
        .count()
    review_count = worker_session.query(entity_table.c.entity_id.distinct())\
        .filter(entity_table.c.clustered == False)\
        .count()
    sel = ''' 
        SELECT 
            entity_id, 
            MAX(confidence)::DOUBLE PRECISION,
            COUNT(*)
        FROM "entity_{0}"
        WHERE clustered = FALSE
        GROUP BY entity_id
    '''.format(session_id)
    clusters = list(engine.execute(sel))
    examples = {c[0]:{'attributes':c[1:], 'label': None, 'score': 1.0} \
        for c in clusters}
    machine = ReviewMachine(examples)
    dd = worker_session.query(DedupeSession).get(session_id)
    dd.review_machine = cPickle.dumps(machine)
    dd.entity_count = entity_count
    dd.review_count = review_count
    dd.status = 'entity map updated'
    worker_session.add(dd)
    worker_session.commit()
    return 'ok'

@queuefunc
def dedupeCanon(session_id, threshold=0.25):
    dd = worker_session.query(DedupeSession).get(session_id)
    engine = worker_session.bind
    metadata = MetaData()
    writeCanonRep(session_id)
    writeProcessedTable(session_id, 
                        proc_table_format='processed_{0}_cr', 
                        raw_table_format='cr_{0}')
    entity_table_name = 'entity_{0}_cr'.format(session_id)
    entity_table = entity_map(entity_table_name, metadata, record_id_type=String)
    entity_table.drop(bind=engine, checkfirst=True)
    entity_table.create(bind=engine)
    block_gen = blockDedupe(session_id, 
        table_name='processed_{0}_cr'.format(session_id), 
        entity_table_name='entity_{0}_cr'.format(session_id), 
        canonical=True)
    writeBlockingMap(session_id, block_gen, canonical=True)
    clustered_dupes = clusterDedupe(session_id, canonical=True, threshold=threshold)
    if clustered_dupes:
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
            try:
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
            except Exception, e: # pragma: no cover
                conn.rollback()
                raise e
    else: # pragma: no cover
        print 'did not find clusters'
        getMatchingReady(session_id)
    review_count = worker_session.query(entity_table.c.entity_id.distinct())\
        .filter(entity_table.c.clustered == False)\
        .count()
    sel = ''' 
        SELECT 
            entity_id, 
            MAX(confidence)::DOUBLE PRECISION,
            COUNT(*)
        FROM "entity_{0}_cr"
        WHERE clustered = FALSE
        GROUP BY entity_id
    '''.format(session_id)
    clusters = list(engine.execute(sel))
    examples = {c[0]:{'attributes':c[1:], 'label': None, 'score': 1.0} \
        for c in clusters}
    machine = ReviewMachine(examples)
    dd.review_machine = cPickle.dumps(machine)
    dd.review_count = review_count
    dd.status = 'canon clustered'
    worker_session.add(dd)
    worker_session.commit()
    return 'ok'
