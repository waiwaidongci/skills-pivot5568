"""月末关账：按北京时间月末时点锁定各渠道整月批次，逐月补算并合算招生月报。

关账分两段提交，保证并发关账、中断续跑和重试都沿用已锁定的批次：
1. 锁定段：写入关账记录（月份唯一）和各渠道关账时点前最后入库的批次。
2. 合算段：持行锁读取锁定来源，缺渠道记缺数不出月报，否则合算并生成一次运行。
"""
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.config.database import Session
from src.constants.enums import Closing, Review
from src.domain.spec import CHANNELS, MIN_OUTCOME_COUNT
from src.models.jobs import ClosingConfig, ClosingSource, Run
from src.models.jobs import Closing as ClosingRow
from src.models.source import ChannelBatch, Dataset
from src.services.audit import record
from src.services.datasets import save
from src.utils.time import iso, now

log = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
CLOSER = 'closer'


def beijing_now(stamp=None):
    return (stamp or now()).replace(tzinfo=UTC).astimezone(BEIJING)


def valid_period(text, label='月份'):
    try:
        if not isinstance(text, str) or len(text) != 7 or text[4] != '-':
            raise ValueError
        year, month = (int(part) for part in text.split('-'))
        if not 2000 <= year <= 2100 or not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        raise ValueError(f'{label}须为 YYYY-MM 格式') from None
    return text


def closing_point(period):
    """关账时点：该月次月 1 日 00:00（北京时间），换算为 UTC 朴素时间存储。"""
    year, month = (int(part) for part in period.split('-'))
    year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return datetime(year, month, 1, tzinfo=BEIJING).astimezone(UTC).replace(tzinfo=None)


def due_periods(start, stamp=None):
    """从关账起月逐月列出已过关账时点的月份（停机漏掉的月份在此补算）。"""
    stamp = stamp or now()
    periods = []
    year, month = (int(part) for part in start.split('-'))
    while closing_point(f'{year:04d}-{month:02d}') <= stamp:
        periods.append(f'{year:04d}-{month:02d}')
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return periods


def check_batch_rows(channel, period, rows):
    """验收渠道整月批次：渠道在名册内、月份不晚于当前月、行内容单渠道单整月。"""
    if channel not in CHANNELS:
        raise ValueError('招生渠道不在公开名册内')
    valid_period(period, '批次月份')
    if period > beijing_now().strftime('%Y-%m'):
        raise ValueError('批次月份不能晚于当前月份')
    for row in rows:
        if row['channel'] != channel:
            raise ValueError(f'整月批次只验收{channel}的受理记录，请检查 CSV 渠道列')
        if not row['intake_date'].startswith(period):
            raise ValueError(f'整月批次只验收 {period} 的受理记录，请检查受理日期')


def _lock_sources(db, closing):
    """取各渠道在关账时点前最后入库的一批；缺渠道留空记缺数。"""
    for channel in CHANNELS:
        batch = db.scalar(select(ChannelBatch)
                          .where(ChannelBatch.channel == channel,
                                 ChannelBatch.period == closing.period,
                                 ChannelBatch.created_at < closing.due_at)
                          .order_by(ChannelBatch.created_at.desc(), ChannelBatch.id.desc()).limit(1))
        db.add(ClosingSource(closing_id=closing.id, channel=channel, batch_id=batch.id if batch else None))


def _ensure_closing(db, period):
    closing = db.scalar(select(ClosingRow).where(ClosingRow.period == period))
    if closing:
        return closing
    closing = ClosingRow(period=period, due_at=closing_point(period), author=CLOSER)
    try:
        with db.begin_nested():
            db.add(closing)
            db.flush()
    except IntegrityError:
        # 并发关账：另一事务已锁定该月，沿用其关账记录和锁定批次。
        return db.scalar(select(ClosingRow).where(ClosingRow.period == period).with_for_update())
    _lock_sources(db, closing)
    record(db, CLOSER, 'closing.locked', closing.id, period=period)
    db.flush()
    return closing


def _finalize(db, closing):
    """在持有该关账记录行锁的事务内调用；已合算的月份直接跳过，重试不重复生成。"""
    if closing.state != Closing.LOCKED:
        return False
    sources = db.scalars(select(ClosingSource).where(ClosingSource.closing_id == closing.id)
                         .order_by(ClosingSource.id)).all()
    missing = [source.channel for source in sources if source.batch_id is None]
    if missing:
        closing.state = Closing.INCOMPLETE
        closing.missing = missing
        closing.finished_at = now()
        record(db, CLOSER, 'closing.incomplete', closing.id, period=closing.period, missing=missing)
        return True
    rows = []
    for source in sources:
        batch = db.get(ChannelBatch, source.batch_id)
        rows.extend(db.get(Dataset, batch.dataset_id).rows)
    dataset, _ = save(db, f'关账合算 {closing.period}', f'closing-{closing.period}.csv', rows, CLOSER)
    run = Run(dataset_id=dataset.id, name=f'招生月报 {closing.period}',
              params={'period': closing.period, 'min_count': MIN_OUTCOME_COUNT}, author=CLOSER)
    db.add(run)
    db.flush()
    closing.run_id = run.id
    closing.state = Closing.CLOSED
    closing.finished_at = now()
    record(db, CLOSER, 'closing.closed', closing.id, period=closing.period, run_id=run.id)
    return True


def close_due(limit=36):
    """逐月补算到期月份；上传照常进行，迟到批次不影响已过的关账时点。"""
    with Session.begin() as db:
        start = db.scalar(select(ClosingConfig.start_month).where(ClosingConfig.id == 1))
    if not start:
        return
    for period in due_periods(start)[:limit]:
        try:
            with Session.begin() as db:
                closing = _ensure_closing(db, period)
            with Session.begin() as db:
                _finalize(db, db.get(ClosingRow, closing.id, with_for_update=True))
        except Exception:
            log.exception('Closing %s failed', period)
            break


def set_start_month(db, month, actor):
    month = valid_period(month, '关账起月')
    if month > beijing_now().strftime('%Y-%m'):
        raise ValueError('关账起月不能晚于当前月份')
    config = db.get(ClosingConfig, 1, with_for_update=True)
    if config:
        config.start_month, config.author, config.updated_at = month, actor, now()
    else:
        db.add(ClosingConfig(id=1, start_month=month, author=actor))
    return month


def _source_view(channel, batch, dataset, masked):
    if batch is None:
        return {'channel': channel, 'received': False}
    entry = {'channel': channel, 'received': True}
    if not masked:
        entry.update(batch=dataset.name, uploaded_at=iso(batch.created_at), dataset_id=dataset.id)
    return entry


def overview(db, masked=False):
    """公开口径页的每月来源和结果；只读角色隐去源批次细节。"""
    config = db.get(ClosingConfig, 1)
    start = config.start_month if config else None
    closings = {row.period: row for row in db.scalars(select(ClosingRow)).all()}
    periods = set(closings)
    if start:
        current = beijing_now().strftime('%Y-%m')
        year, month = (int(part) for part in start.split('-'))
        while f'{year:04d}-{month:02d}' <= current:
            periods.add(f'{year:04d}-{month:02d}')
            year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    if not periods:
        return {'start_month': start, 'months': []}
    batches = db.scalars(select(ChannelBatch).where(ChannelBatch.period.in_(periods))
                         .order_by(ChannelBatch.created_at, ChannelBatch.id)).all()
    datasets = {item.id: item for item in db.scalars(
        select(Dataset).where(Dataset.id.in_({item.dataset_id for item in batches} or {0})))}
    sources = db.scalars(select(ClosingSource).where(ClosingSource.closing_id.in_(
        [row.id for row in closings.values()] or [0])).order_by(ClosingSource.id)).all()
    locked = defaultdict(dict)
    for source in sources:
        locked[source.closing_id][source.channel] = source.batch_id
    runs = {row.id: row for row in db.scalars(
        select(Run).where(Run.id.in_([row.run_id for row in closings.values() if row.run_id] or [0])))}
    by_id = {item.id: item for item in batches}
    by_period = defaultdict(list)
    for batch in batches:
        by_period[batch.period].append(batch)

    months = []
    for period in sorted(periods, reverse=True):
        closing = closings.get(period)
        due = closing.due_at if closing else closing_point(period)
        received = [batch for batch in by_period.get(period, []) if batch.created_at < due]
        late = [batch for batch in by_period.get(period, []) if batch.created_at >= due]
        if closing:
            state, picked = closing.state, locked[closing.id]
        else:
            state = 'queued' if due <= now() else 'waiting'
            picked = {batch.channel: batch.id for batch in received}
        entries = []
        for channel in CHANNELS:
            batch = by_id.get(picked.get(channel))
            entries.append(_source_view(channel, batch, datasets.get(batch.dataset_id) if batch else None, masked))
        run = runs.get(closing.run_id) if closing and closing.run_id else None
        published = run and (not masked or run.review == Review.ACCEPTED)
        months.append({
            'period': period,
            'due_at': iso(due),
            'state': state,
            'missing': closing.missing if closing else [c for c in CHANNELS if not picked.get(c)],
            'sources': entries,
            'late': [] if masked else [{'channel': item.channel, 'batch': datasets[item.dataset_id].name,
                                        'uploaded_at': iso(item.created_at)} for item in late],
            'run_id': run.id if published else None,
            'run_status': run.status if published else None,
            'run_review': run.review if published else None,
        })
    return {'start_month': start, 'months': months}
