from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.source import ChannelBatch, Dataset
from src.schemas.requests import ChannelBatchUpload, ClosingConfigRequest
from src.services import closing
from src.services.audit import record
from src.services.datasets import save
from src.services.validation import parse_rows
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/closings')
def months(db: Session = Depends(connection), actor: User = Depends(current)):
    return closing.overview(db, masked=actor.role == Role.VIEWER)


@router.post('/closings/config')
def configure(body: ClosingConfigRequest, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    month = closing.set_start_month(db, body.start_month, actor.username)
    record(db, actor.username, 'closing.configured', month)
    db.commit()
    try:
        closing.close_due()
    except Exception:
        # 配置已保存，后台 worker 会继续逐月补算。
        pass
    wake()
    return {'start_month': month}


@router.get('/batches')
def batches(db: Session = Depends(connection), actor: User = Depends(current)):
    if actor.role == Role.VIEWER:
        return []
    items = db.scalars(select(ChannelBatch).order_by(ChannelBatch.id.desc()).limit(50)).all()
    datasets = {item.id: item for item in db.scalars(
        select(Dataset).where(Dataset.id.in_({batch.dataset_id for batch in items} or {0})))}
    return [views.channel_batch(batch, datasets[batch.dataset_id]) for batch in items]


@router.post('/batches', status_code=201)
def upload(body: ChannelBatchUpload, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    rows = parse_rows(body.csv_text)
    closing.check_batch_rows(body.channel, body.period, rows)
    name = body.name or f'{body.channel} {body.period} 整月批次'
    dataset, _ = save(db, name, body.filename, rows, actor.username)
    batch = ChannelBatch(dataset_id=dataset.id, channel=body.channel, period=body.period,
                         author=actor.username)
    db.add(batch)
    db.flush()
    record(db, actor.username, 'batch.uploaded', batch.id,
           channel=batch.channel, period=batch.period, rows=dataset.row_count)
    db.commit()
    wake()
    return views.channel_batch(batch, dataset)
