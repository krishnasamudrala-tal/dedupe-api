from sqlalchemy import String, Integer, LargeBinary, ForeignKey, Boolean, \
    Column, Table, Float
from sqlalchemy.orm import relationship, backref, synonym
from api.database import Base, engine, session
from flask_bcrypt import Bcrypt
from uuid import uuid4
from datetime import datetime, timedelta

bcrypt = Bcrypt()

def get_checkout_expiration():
    return datetime.now() + timedelta(minutes=10)

def entity_map(name, metadata, pk_type=Integer):
    table = Table(name, metadata, 
        Column('record_id', pk_type, primary_key=True),
        Column('group_id', Integer), 
        Column('confidence', Float(precision=50)),
        Column('clustered', Boolean),
        Column('checked_out', Boolean),
        Column('checkout_expire', Datetime, default=get_checkout_expiration),
        extend_existing=True
    )
    return table

def block_map_table(name, metadata, pk_type=Integer):
    table = Table(name, metadata,
        Column('block_key', String(200)),
        Column('record_id', pk_type),
        extend_existing=True
    )
    return table

def get_uuid():
    return unicode(uuid4())

class DedupeSession(Base):
    __tablename__ = 'dedupe_session'
    id = Column(String, default=get_uuid, primary_key=True)
    name = Column(String, nullable=False)
    training_data = Column(LargeBinary)
    settings_file = Column(LargeBinary)
    field_defs = Column(LargeBinary)
    conn_string = Column(String)
    group_id = Column(String(36), ForeignKey('group.id'))
    group = relationship('Group', backref=backref('sessions'))

    def __repr__(self):
        return '<DedupeSession %r >' % (self.name)
    
    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

roles_users = Table('role_users', Base.metadata,
    Column('user_id', String(36), ForeignKey('user.id')),
    Column('role_id', Integer, ForeignKey('role.id'))
)

groups_users = Table('group_users', Base.metadata,
    Column('user_id', String(36), ForeignKey('user.id')),
    Column('group_id', String(36), ForeignKey('group.id'))
)

class Role(Base):
    __tablename__ = 'role'
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True)
    description = Column(String(255))
    
    def __repr__(self):
        return '<Role %r>' % self.name

class Group(Base):
    __tablename__ = 'group'
    id = Column(String(36), default=get_uuid, primary_key=True)
    name = Column(String(10))
    description = Column(String(255))

    def __repr__(self):
        return '<Group %r>' % self.name

class User(Base):
    __tablename__ = 'user'
    id = Column(String(36), default=get_uuid, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    active = Column(Boolean())
    _password = Column('password', String, nullable=False)
    roles = relationship('Role', secondary=roles_users,
        backref=backref('users', lazy='dynamic'))
    groups = relationship('Group', secondary=groups_users,
        backref=backref('users', lazy='dynamic'))
    
    def __repr__(self):
        return '<User %r>' % self.name

    def _get_password(self):
        return self._password
    
    def _set_password(self, value):
        self._password = bcrypt.generate_password_hash(value)

    password = property(_get_password, _set_password)
    password = synonym('_password', descriptor=password)

    def __init__(self, name, password, email):
        self.name = name
        self.password = password
        self.email = email

    @classmethod
    def get_by_username(cls, name):
        return session.query(cls).filter(cls.name == name).first()

    @classmethod
    def check_password(cls, name, value):
        user = cls.get_by_username(name)
        if not user:
            return False
        return bcrypt.check_password_hash(user.password, value)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id
