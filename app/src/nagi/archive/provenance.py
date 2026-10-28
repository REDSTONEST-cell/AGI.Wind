from sqlalchemy import Table,Column,Text,UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from .models import metadata,idcol,fk

archive_origins=Table("archive_origins",metadata,idcol(),fk("companion_id","companions"),Column("entity_table",Text,nullable=False),Column("target_id",UUID(as_uuid=True),nullable=False),Column("origin_id",UUID(as_uuid=True),nullable=False),UniqueConstraint("entity_table","target_id"))
