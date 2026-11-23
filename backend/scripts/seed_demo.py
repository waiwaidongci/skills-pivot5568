import json
from datetime import datetime
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import NOVEMBER, OCTOBER, channel_month_rows, demo_rows
from src.domain.spec import CHANNELS, SPEC
from src.models.identity import User
from src.models.jobs import ClosingConfig
from src.models.source import ChannelBatch, Dataset
from src.services.datasets import save
from src.utils.security import hash_password

# 演示入库时间（UTC 朴素时间）：关账时点前入库的纳入合算，之后的记为迟到。
_BATCH_PLAN = (
    ('2026-09', CHANNELS[:2], NOVEMBER, datetime(2026, 9, 27, 12)),   # 缺就业服务站，演示缺数
    ('2026-10', CHANNELS, OCTOBER, datetime(2026, 10, 28, 12)),
    ('2026-11', CHANNELS, NOVEMBER, datetime(2026, 11, 20, 12)),      # 未到关账时点
)


def _seed_channel_batches(db):
    if db.scalar(select(func.count()).select_from(ChannelBatch)):
        return 0
    count = 0
    for period, channels, grid, stored_at in _BATCH_PLAN:
        for channel in channels:
            rows = channel_month_rows(period, channel, grid)
            dataset, _ = save(db, f'{channel} {period} 整月批次', f'{channel}_{period}.csv', rows, 'seed')
            db.add(ChannelBatch(dataset_id=dataset.id, channel=channel, period=period,
                                author='seed', created_at=stored_at))
            count += 1
    # 跨过关账时点才入库的 10 月补传批次，演示迟到不纳入。
    late = channel_month_rows('2026-10', CHANNELS[0], OCTOBER)
    dataset, _ = save(db, f'{CHANNELS[0]} 2026-10 整月批次（补传）', f'{CHANNELS[0]}_2026-10_补传.csv',
                      late, 'seed')
    db.add(ChannelBatch(dataset_id=dataset.id, channel=CHANNELS[0], period='2026-10',
                        author='seed', created_at=datetime(2026, 11, 2, 12)))
    return count + 1


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        batches = _seed_channel_batches(db)
        if not db.get(ClosingConfig, 1):
            db.add(ClosingConfig(id=1, start_month='2026-09', author='seed'))
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created, 'channel_batches': batches}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
