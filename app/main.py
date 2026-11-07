"""SOAST: ticket correlation, RBAC, SLA and coordination API.

Tickets are read-only projections of the proprietary source. No endpoint changes
legacy ownership or status. All timestamps stored in UTC.
"""
from __future__ import annotations

import csv
import hashlib
import io
import hmac
import os
import secrets
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .graph import GraphError, graph_configured, graph_request, events_path
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

UTC = timezone.utc
DB_URL = os.getenv("SOAST_DATABASE_URL", "sqlite:///./soast.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, pool_pre_ping=True)
DbSession = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(24))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    level: Mapped[str | None] = mapped_column(String(2), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("company_id", "legacy_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    legacy_id: Mapped[str] = mapped_column(String(40), index=True)
    type: Mapped[str] = mapped_column(String(16))
    priority: Mapped[str] = mapped_column(String(10))
    product: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    complete_history: Mapped[bool] = mapped_column(Boolean, default=False)
    third_party_justification: Mapped[str | None] = mapped_column(String(500), nullable=True)

class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (UniqueConstraint("source", "source_seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    source: Mapped[str] = mapped_column(String(150))
    source_seq: Mapped[str] = mapped_column(String(60))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20))
    owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    level: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    activity: Mapped[str | None] = mapped_column(String(12), nullable=True)

class AssignmentProposal(Base):
    __tablename__ = "assignment_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"),index=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=lambda:datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20),default="proposed")
    rule: Mapped[str] = mapped_column(String(160))

class ImportIssue(Base):
    __tablename__ = "import_issues"
    __table_args__ = (UniqueConstraint("source", "source_seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(150))
    source_seq: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(String(500))

class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("user_id", "weekday", "start_minute", "end_minute"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    weekday: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)


class DatedShift(Base):
    """A dated shift imported from a monthly roster. Never repeats weekly."""
    __tablename__ = "dated_shifts"
    __table_args__ = (UniqueConstraint("user_id", "work_date", "start_minute", "end_minute", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(25))
    source: Mapped[str] = mapped_column(String(150))
    source_line: Mapped[int] = mapped_column(Integer)

class ScheduleImportCoverage(Base):
    """Day ranges for which dated rosters override recurring shifts, including days off."""
    __tablename__ = "schedule_import_coverage"
    __table_args__ = (UniqueConstraint("source", "start_day", "end_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(150))
    start_day: Mapped[date] = mapped_column(Date, index=True)
    end_day: Mapped[date] = mapped_column(Date, index=True)

class Absence(Base):
    __tablename__ = "absences"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(200))

class SupportSession(Base):
    __tablename__ = "support_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(35), default="sync_pending")
    teams_url: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    graph_event_id: Mapped[str | None] = mapped_column(String(250), nullable=True)
    graph_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    invitee_email: Mapped[str] = mapped_column(String(250))
    graph_attempts: Mapped[int] = mapped_column(Integer,default=0)
    graph_last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)

class SessionSlot(Base):
    __tablename__ = "session_slots"
    __table_args__ = (UniqueConstraint("technician_id", "slot_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("support_sessions.id"))
    slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(1000), default="")

Base.metadata.create_all(engine)
app = FastAPI(title="SOAST / TEECMO", version="0.1.0")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
ROLES = {"externo", "tecnico", "supervisor", "coordinador", "gerencia", "administrador"}
STATES = {"COLA", "DIAGNOSTICO", "PROGRESO", "ESPERA", "RESUELTO", "CERRADO"}


def now() -> datetime:
    return datetime.now(UTC)

def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()

def valid_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split(":")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)).split(":")[1], digest)
    except (ValueError, TypeError):
        return False

def db():
    with DbSession() as session:
        yield session

def audit(db: Session, user: User | None, action: str, entity: str, key: str, detail: str = ""):
    db.add(Audit(actor_id=user.id if user else None, action=action, entity=entity, entity_id=str(key), detail=detail[:1000]))

def require_user(request: Request, db: Session = Depends(db)) -> User:
    raw = request.headers.get("authorization", "")
    if not raw.startswith("Bearer "):
        raise HTTPException(401, "Autenticación requerida")
    token = raw[7:]
    if len(token) < 32:
        raise HTTPException(401, "Token inválido")
    record = db.scalar(select(AuthSession).where(AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    current = now()
    if not record or record.revoked or current - utc(record.created_at) >= timedelta(hours=8) or current - utc(record.last_seen) >= timedelta(minutes=30):
        raise HTTPException(401, "Sesión expirada")
    user = db.get(User, record.user_id)
    if not user or not user.active or (user.role == "externo" and (not db.get(Company, user.company_id) or not db.get(Company, user.company_id).active)):
        raise HTTPException(403, "Cuenta o empresa desactivada; contacte a su asesor")
    record.last_seen = current
    db.commit()
    return user

def permit(user: User, *roles: str):
    if user.role not in roles:
        raise HTTPException(403, "Acción no autorizada para este rol")

def scoped_ticket(db: Session, user: User, ticket_id: int) -> Ticket:
    permit(user, "externo", "tecnico", "supervisor", "coordinador")
    ticket = db.get(Ticket, ticket_id)
    if not ticket or (user.role == "externo" and ticket.company_id != user.company_id):
        raise HTTPException(404, "Ticket no encontrado")
    return ticket

def session_dict(db: Session, s: SupportSession) -> dict:
    t = db.get(Ticket, s.ticket_id)
    return {"id":s.id,"ticket_id":s.ticket_id,"ticket":t.legacy_id,"company_id":t.company_id,
            "technician":db.get(User,s.technician_id).username,"starts_at":utc(s.starts_at).isoformat(),
            "ends_at":utc(s.ends_at).isoformat(),"state":s.state,"teams_url":s.teams_url,
            "graph_error":s.graph_error}

def sla(db: Session, ticket: Ticket, at: datetime | None = None) -> dict:
    events = db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id).order_by(TicketEvent.occurred_at, TicketEvent.id)).all()
    if not events or not ticket.complete_history:
        return {"state":"sin_datos", "hours":None,"limit":4 if ticket.type == "INCIDENTE" else 24,"ratio":None}
    reference = utc(at or now())
    elapsed = 0.0
    for i, e in enumerate(events):
        start = utc(e.occurred_at)
        end = utc(events[i+1].occurred_at) if i+1 < len(events) else reference
        if e.status in {"COLA", "DIAGNOSTICO", "PROGRESO"}:
            elapsed += max(0, (min(end,reference)-start).total_seconds())
    hours = round(elapsed/3600, 3)
    limit = 4 if ticket.type == "INCIDENTE" else 24
    ratio = hours/limit
    if ticket.status in {"RESUELTO", "CERRADO"}:
        state = "cumplido" if hours <= limit else ("justificado_tercero" if ticket.third_party_justification else "incumplido")
    elif ratio >= 1:
        state = "vencido"
    elif ratio >= .8:
        state = "por_vencer"
    elif ratio >= .6:
        state = "atrasado"
    else:
        state = "en_plazo"
    return {"state":state,"hours":hours,"limit":limit,"ratio":round(ratio,4)}

class LoginInput(BaseModel):
    username: str
    password: str
class UserInput(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=200)
    role: str
    company_id: int | None = None
    level: str | None = None
    email: str | None = None
class CompanyInput(BaseModel):
    code: str = Field(min_length=1,max_length=30)
    name: str = Field(min_length=2,max_length=200)
class ShiftInput(BaseModel):
    user_id: int
    weekday: int = Field(ge=0,le=6)
    start_minute: int = Field(ge=0,le=1439)
    end_minute: int = Field(ge=1,le=1440)
class AbsenceInput(BaseModel):
    user_id: int
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=3,max_length=200)
class SessionInput(BaseModel):
    ticket_id: int
    starts_at: datetime
    duration: int
    invitee_email: str = Field(min_length=5,max_length=250)
class RebookInput(BaseModel):
    starts_at: datetime
    duration: int
class LinkInput(BaseModel):
    teams_url: str
class JustificationInput(BaseModel):
    reason: str = Field(min_length=8,max_length=500)
class PasswordInput(BaseModel):
    password: str = Field(min_length=12,max_length=200)

@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")

@app.get("/api/health")
def health(db: Session = Depends(db)):
    return {"service":"ok", "database":"ok", "graph_configured":graph_configured(),
            "events":db.scalar(select(func.count()).select_from(TicketEvent))}

@app.post("/api/bootstrap")
def bootstrap(db: Session = Depends(db)):
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "Inicialización ya realizada")
    username, password = os.getenv("SOAST_ADMIN_USERNAME"), os.getenv("SOAST_ADMIN_PASSWORD")
    if not username or not password or len(password) < 12 or password == "REEMPLAZAR_CON_UNA_CLAVE_SEGURA":
        raise HTTPException(503, "Configure SOAST_ADMIN_USERNAME y SOAST_ADMIN_PASSWORD (12 caracteres mínimo)")
    user = User(username=username, password_hash=hash_password(password),role="administrador")
    db.add(user); db.flush(); audit(db,user,"bootstrap","user",str(user.id));db.commit()
    return {"initialized":True}

@app.post("/api/login")
def login(payload: LoginInput, db: Session = Depends(db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not user.active or not valid_password(payload.password, user.password_hash):
        raise HTTPException(401, "Credenciales inválidas")
    if user.role == "externo":
        company = db.get(Company,user.company_id)
        if not company or not company.active: raise HTTPException(403,"Empresa desactivada; contacte a su asesor")
    token = secrets.token_urlsafe(48)
    t = now()
    db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,created_at=t,last_seen=t))
    audit(db,user,"login","user",str(user.id));db.commit()
    return {"token":token,"role":user.role,"username":user.username}

@app.post("/api/logout")
def logout(request: Request, user: User = Depends(require_user), db: Session = Depends(db)):
    token = request.headers["authorization"][7:]
    record = db.scalar(select(AuthSession).where(AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    record.revoked = True; audit(db,user,"logout","user",str(user.id));db.commit()
    return {"ok":True}

@app.get("/api/me")
def me(user: User = Depends(require_user)):
    return {"username":user.username,"role":user.role,"company_id":user.company_id,"level":user.level}

@app.get("/api/companies")
def companies(user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user,"administrador","supervisor","coordinador")
    return [{"id":c.id,"code":c.code,"name":c.name,"active":c.active} for c in db.scalars(select(Company).order_by(Company.name)).all()]

@app.post("/api/companies")
def add_company(payload: CompanyInput, user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user,"administrador")
    company = Company(code=payload.code.strip(),name=payload.name.strip())
    db.add(company)
    try: db.flush()
    except IntegrityError: db.rollback();raise HTTPException(409,"Código ya registrado")
    audit(db,user,"create","company",str(company.id));db.commit()
    return {"id":company.id}

@app.patch("/api/companies/{company_id}/active")
def company_active(company_id: int, active: bool, user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user,"administrador")
    company = db.get(Company, company_id)
    if not company:raise HTTPException(404,"Empresa no encontrada")
    company.active = active; audit(db,user,"status","company",str(company.id),str(active));db.commit()
    return {"ok":True}

@app.get("/api/users")
def users(user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user,"administrador","supervisor","coordinador")
    query=select(User)
    if user.role != "administrador": query=query.where(User.role == "tecnico")
    return [{"id":u.id,"username":u.username,"role":u.role,"company_id":u.company_id,"level":u.level,"active":u.active} for u in db.scalars(query.order_by(User.username)).all()]

@app.post("/api/users")
def add_user(payload: UserInput,user: User = Depends(require_user),db: Session = Depends(db)):
    permit(user,"administrador")
    if payload.role not in ROLES or (payload.role == "externo") != (payload.company_id is not None):
        raise HTTPException(422,"Rol y empresa incompatibles")
    if payload.role == "tecnico" and payload.level not in {"L1","L2","L3"}:
        raise HTTPException(422,"Nivel técnico requerido")
    if payload.company_id is not None and (not (company:=db.get(Company,payload.company_id)) or not company.active):
        raise HTTPException(422,"Empresa inexistente o inactiva")
    created=User(username=payload.username.strip(),password_hash=hash_password(payload.password),role=payload.role,
                 company_id=payload.company_id,level=payload.level if payload.role=="tecnico" else None,email=payload.email)
    db.add(created)
    try:db.flush()
    except IntegrityError:db.rollback();raise HTTPException(409,"Nombre de usuario existente")
    audit(db,user,"create","user",str(created.id),created.role);db.commit()
    return {"id":created.id}

@app.patch("/api/users/{user_id}/active")
def user_active(user_id:int, active:bool,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"administrador")
    target=db.get(User,user_id)
    if not target:raise HTTPException(404,"Usuario no encontrado")
    if target.id==user.id and not active:raise HTTPException(409,"No puede desactivarse")
    target.active=active; audit(db,user,"status","user",str(target.id),str(active));db.commit()
    return {"ok":True}

@app.post("/api/users/{user_id}/password")
def reset_password(user_id:int,payload:PasswordInput,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"administrador")
    target=db.get(User,user_id)
    if not target:raise HTTPException(404,"Usuario no encontrado")
    target.password_hash=hash_password(payload.password)
    db.query(AuthSession).filter(AuthSession.user_id==target.id).update({AuthSession.revoked:True})
    audit(db,user,"reset_password","user",str(target.id));db.commit()
    return {"ok":True}

@app.get("/api/tickets")
def tickets(q: str="", company_id:int|None=None, limit:int=100, offset:int=0,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","tecnico","supervisor","coordinador")
    stmt=select(Ticket)
    if user.role=="externo":stmt=stmt.where(Ticket.company_id==user.company_id)
    elif company_id is not None:stmt=stmt.where(Ticket.company_id==company_id)
    if q:stmt=stmt.where(Ticket.legacy_id.contains(q[:40]))
    limit=max(1,min(limit,100));offset=max(0,offset)
    items=db.scalars(stmt.order_by(Ticket.last_seen.desc()).limit(limit).offset(offset)).all()
    return [{"id":t.id,"legacy_id":t.legacy_id,"company_id":t.company_id,"type":t.type,"priority":t.priority,
             "product":t.product,"status":t.status,"level":t.level,"owner":t.owner,"sla":sla(db,t)} for t in items]

@app.get("/api/tickets/{ticket_id}")
def ticket_detail(ticket_id:int,user:User=Depends(require_user),db:Session=Depends(db)):
    t=scoped_ticket(db,user,ticket_id)
    events=db.scalars(select(TicketEvent).where(TicketEvent.ticket_id==t.id).order_by(TicketEvent.occurred_at,TicketEvent.id)).all()
    return {"id":t.id,"legacy_id":t.legacy_id,"company_id":t.company_id,"status":t.status,"owner":t.owner,"level":t.level,
            "type":t.type,"priority":t.priority,"product":t.product,"sla":sla(db,t),
            "complete_history":t.complete_history,
            "events":[{"date":utc(e.occurred_at).isoformat(),"status":e.status,"owner":e.owner,"level":e.level,"actor":e.actor,"activity":e.activity} for e in events]}

@app.post("/api/tickets/{ticket_id}/third-party")
def third_party(ticket_id:int,payload:JustificationInput,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"supervisor","coordinador")
    t=db.get(Ticket,ticket_id)
    if not t:raise HTTPException(404,"Ticket no encontrado")
    if sla(db,t)["state"]!="incumplido":raise HTTPException(409,"Solo se clasifican casos resueltos incumplidos")
    t.third_party_justification=payload.reason
    audit(db,user,"justify_third_party","ticket",str(t.id),payload.reason);db.commit()
    return {"ok":True}

@app.get("/api/dashboard")
def dashboard(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","tecnico","supervisor","coordinador","gerencia")
    stmt=select(Ticket)
    if user.role=="externo":stmt=stmt.where(Ticket.company_id==user.company_id)
    elif user.role=="tecnico":stmt=stmt.where(Ticket.owner==user.username)
    all_t=db.scalars(stmt).all()
    counts={"abiertos":0,"en_progreso":0,"atrasados":0,"por_vencer":0,"cumplidos":0,"incumplidos":0,"sin_datos":0}
    for t in all_t:
        x=sla(db,t)["state"]
        if x=="sin_datos":counts["sin_datos"]+=1
        if x in {"cumplido","incumplido","justificado_tercero"}:
            if x=="cumplido":counts["cumplidos"]+=1
            if x=="incumplido":counts["incumplidos"]+=1
            continue
        if x=="atrasado":counts["atrasados"]+=1
        elif x in {"por_vencer","vencido"}:counts["por_vencer"]+=1
        elif t.status=="PROGRESO":counts["en_progreso"]+=1
        else:counts["abiertos"]+=1
    denominator=counts["cumplidos"]+counts["incumplidos"]
    return {"total":len(all_t),"counts":counts,"compliance_pct":round(counts["cumplidos"]*100/denominator,2) if denominator else None}

def csv_cell(value):
    text = str(value if value is not None else "")
    return "'" + text if text and text[0] in "=+-@\t\r" else text

@app.get("/api/reports/tickets.csv")
def export_tickets(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","tecnico","supervisor","coordinador","gerencia")
    stmt=select(Ticket)
    if user.role=="externo":stmt=stmt.where(Ticket.company_id==user.company_id)
    if user.role=="tecnico":stmt=stmt.where(Ticket.owner==user.username)
    buffer=io.StringIO();writer=csv.writer(buffer)
    writer.writerow(["empresa","ticket","tipo","producto","prioridad","estado","nivel","owner","sla_estado","sla_horas"])
    for t in db.scalars(stmt.order_by(Ticket.id)):
        metric=sla(db,t)
        writer.writerow([csv_cell(x) for x in [t.company_id,t.legacy_id,t.type,t.product,t.priority,t.status,t.level,t.owner,metric["state"],metric["hours"]]])
    audit(db,user,"export_csv","tickets","all","Ámbito por rol aplicado");db.commit()
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]),media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition":"attachment; filename=soast-tickets.csv"})

@app.get("/api/escalations")
def escalations(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","tecnico","supervisor","coordinador")
    stmt=select(Ticket)
    if user.role=="externo":stmt=stmt.where(Ticket.company_id==user.company_id)
    routes={("L1","L2"),("L2","L3"),("L2","FABRICA"),("L3","FABRICA")}
    result=[]
    for ticket in db.scalars(stmt):
        events=db.scalars(select(TicketEvent).where(TicketEvent.ticket_id==ticket.id).order_by(TicketEvent.occurred_at,TicketEvent.id)).all()
        for previous,current in zip(events,events[1:]):
            if current.level!=previous.level:
                result.append({"ticket_id":ticket.id,"ticket":ticket.legacy_id,"company_id":ticket.company_id,
                               "from_level":previous.level,"to_level":current.level,
                               "valid_route":(previous.level,current.level) in routes,
                               "at":utc(current.occurred_at).isoformat()})
    return result[-200:]

@app.get("/api/shifts")
def shifts(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"supervisor","coordinador","tecnico")
    query=select(Shift)
    if user.role=="tecnico":query=query.where(Shift.user_id==user.id)
    return [{"id":x.id,"user_id":x.user_id,"weekday":x.weekday,"start_minute":x.start_minute,"end_minute":x.end_minute} for x in db.scalars(query).all()]

@app.post("/api/shifts")
def add_shift(payload:ShiftInput,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"supervisor")
    target=db.get(User,payload.user_id)
    if not target or target.role!="tecnico" or payload.end_minute<=payload.start_minute:
        raise HTTPException(422,"Técnico y horas válidos requeridos")
    s=Shift(**payload.model_dump());db.add(s)
    try:db.flush()
    except IntegrityError:db.rollback();raise HTTPException(409,"Turno duplicado")
    audit(db,user,"create","shift",str(s.id));db.commit()
    return {"id":s.id}


@app.get("/api/schedule-months")
def schedule_months(user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user, "supervisor", "coordinador", "tecnico")
    periods = db.scalars(select(ScheduleImportCoverage).order_by(ScheduleImportCoverage.start_day.desc())).all()
    months = set()
    for period in periods:
        cursor = period.start_day.replace(day=1)
        while cursor <= period.end_day:
            months.add(cursor.isoformat()[:7])
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"months": sorted(months, reverse=True)}

@app.get("/api/dated-shifts")
def dated_shifts(month: str, user: User = Depends(require_user), db: Session = Depends(db)):
    permit(user, "supervisor", "coordinador", "tecnico")
    try:
        month_start = date.fromisoformat(month + "-01")
        if month_start.isoformat()[:7] != month:
            raise ValueError("Mes inválido")
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    except (ValueError, TypeError):
        raise HTTPException(422, "Mes inválido: use AAAA-MM")
    query = (select(DatedShift, User.username, User.active)
             .join(User, DatedShift.user_id == User.id)
             .where(DatedShift.work_date >= month_start, DatedShift.work_date < month_end)
             .order_by(DatedShift.work_date, DatedShift.start_minute, User.username))
    if user.role == "tecnico":
        query = query.where(DatedShift.user_id == user.id)
    rows = db.execute(query.limit(2500)).all()
    return [{"id": shift.id, "user_id": shift.user_id,
             "username": username, "active": active,
             "date": shift.work_date.isoformat(), "kind": shift.kind,
             "start_minute": shift.start_minute, "end_minute": shift.end_minute}
            for shift, username, active in rows]

@app.post("/api/absences")
def add_absence(payload:AbsenceInput,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"supervisor")
    target=db.get(User,payload.user_id)
    if not target or target.role!="tecnico" or utc(payload.end_at)<=utc(payload.start_at):
        raise HTTPException(422,"Ausencia inválida")
    absence=Absence(user_id=target.id,start_at=utc(payload.start_at),end_at=utc(payload.end_at),reason=payload.reason)
    db.add(absence);db.flush();audit(db,user,"create","absence",str(absence.id));db.commit()
    return {"id":absence.id}

LOCAL_TZ=ZoneInfo(os.getenv("SOAST_TIMEZONE","America/Guatemala"))

def candidates(db:Session, ticket:Ticket, start:datetime, end:datetime, exclude_id:int|None=None) -> list[User]:
    start=utc(start);end=utc(end)
    local_start=start.astimezone(LOCAL_TZ);local_end=end.astimezone(LOCAL_TZ)
    if local_start.date()!=local_end.date():return []
    start_min=local_start.hour*60+local_start.minute
    end_min=local_end.hour*60+local_end.minute
    if end_min<=start_min:return []
    levels=[ticket.level] if ticket.level in {"L1","L2","L3"} else []
    users=db.scalars(select(User).where(User.role=="tecnico",User.active.is_(True),User.level.in_(levels))).all()
    roster_covered=bool(db.scalar(select(ScheduleImportCoverage.id).where(
        ScheduleImportCoverage.start_day<=local_start.date(),
        ScheduleImportCoverage.end_day>=local_start.date()).limit(1)))
    selected=[]
    for u in users:
        # A dated roster is authoritative for its covered period: never extrapolate
        # a weekly default into a day off. Guard duty is not a bookable appointment.
        if roster_covered:
            possible=db.scalar(select(DatedShift.id).where(
                DatedShift.user_id==u.id, DatedShift.work_date==local_start.date(),
                DatedShift.kind!="GUARDIA_L3",
                DatedShift.start_minute<=start_min,DatedShift.end_minute>=end_min).limit(1))
        else:
            possible=db.scalar(select(Shift.id).where(Shift.user_id==u.id,Shift.weekday==local_start.weekday(),
                      Shift.start_minute<=start_min,Shift.end_minute>=end_min).limit(1))
        if not possible:continue
        if db.scalar(select(Absence.id).where(Absence.user_id==u.id,Absence.start_at<end,Absence.end_at>start).limit(1)):continue
        if db.scalar(select(SupportSession.id).where(SupportSession.technician_id==u.id,
                     SupportSession.state!="cancelled",SupportSession.id!=exclude_id,
                     SupportSession.starts_at<end,SupportSession.ends_at>start).limit(1)):continue
        selected.append(u)
    # Prefer fewer scheduled sessions this day, then less scheduled time.
    day_start=local_start.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(UTC)
    day_end=(local_start.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).astimezone(UTC)
    def load(u):
        sessions=db.scalars(select(SupportSession).where(SupportSession.technician_id==u.id,
                  SupportSession.state!="cancelled",SupportSession.starts_at>=day_start,SupportSession.starts_at<day_end)).all()
        return len(sessions),sum((utc(x.ends_at)-utc(x.starts_at)).total_seconds() for x in sessions),u.id
    return sorted(selected,key=load)

def slots(start:datetime,end:datetime):
    result=[];cursor=utc(start)
    while cursor<utc(end):
        result.append(cursor);cursor+=timedelta(minutes=15)
    return result

def verify_booking(db:Session,t:Ticket,starts_at:datetime,duration:int,exclude_id:int|None=None):
    start=utc(starts_at)
    if duration not in {30,60,90,120} or start.minute%15 or start.second or start.microsecond:
        raise HTTPException(422,"Duración u hora inválida; inicios cada 15 minutos")
    if start<now()+timedelta(minutes=15):raise HTTPException(422,"Anticipación mínima de 15 minutos")
    if t.status in {"RESUELTO","CERRADO"}:raise HTTPException(409,"Ticket no elegible")
    company=db.get(Company,t.company_id)
    if not company or not company.active:raise HTTPException(409,"Empresa inactiva")
    end=start+timedelta(minutes=duration)
    available=candidates(db,t,start,end,exclude_id)
    if not available:raise HTTPException(409,"Sin técnicos elegibles para ese horario")
    return start,end,available[0]

@app.get("/api/availability")
def availability(ticket_id:int,day:str,duration:int,user:User=Depends(require_user),db:Session=Depends(db)):
    t=scoped_ticket(db,user,ticket_id)
    if duration not in {30,60,90,120}:raise HTTPException(422,"Duración inválida")
    try:date=datetime.strptime(day,"%Y-%m-%d").date()
    except ValueError:raise HTTPException(422,"Fecha inválida")
    beginning=datetime(date.year,date.month,date.day,tzinfo=LOCAL_TZ)
    result=[]
    for minute in range(0,1440,15):
        start=(beginning+timedelta(minutes=minute)).astimezone(UTC)
        end=start+timedelta(minutes=duration)
        if start<now()+timedelta(minutes=15) or end.astimezone(LOCAL_TZ).date()!=date:continue
        if candidates(db,t,start,end):result.append(start.isoformat())
    return {"starts_at":result,"calendar_status":"local_only", "notice":"Disponibilidad corporativa no verificada en Microsoft Graph"}

async def sync_graph(db:Session,s:SupportSession):
    """Synchronize an Outlook event without claiming success until Teams URL exists."""
    if not graph_configured():
        s.graph_error="Microsoft Graph no configurado";db.commit();return
    s.graph_attempts += 1
    s.graph_last_attempt=now()
    db.commit()  # Persist attempts before outbound calls, including network timeouts.
    try:
        base=events_path()
        if s.state=="cancel_pending":
            if not s.graph_event_id:
                raise GraphError("Cancelación requiere conciliación manual: evento sin ID confirmado")
            try:
                await graph_request("POST",f"{base}/{s.graph_event_id}/cancel",json={"comment":"Cancelada por SOAST"})
            except GraphError as exc:
                # An event that no longer exists has no active invitation to cancel.
                if "HTTP 404" not in str(exc):raise
            s.state="cancelled";s.graph_error=None
            db.query(SessionSlot).filter(SessionSlot.session_id==s.id).delete()
            db.commit();return
        if s.state!="sync_pending":return
        if not s.graph_event_id:
            payload={"subject":f"SOAST - Ticket {db.get(Ticket,s.ticket_id).legacy_id}",
                "start":{"dateTime":utc(s.starts_at).replace(tzinfo=None).isoformat(),"timeZone":"UTC"},
                "end":{"dateTime":utc(s.ends_at).replace(tzinfo=None).isoformat(),"timeZone":"UTC"},
                "isOnlineMeeting":True,"onlineMeetingProvider":"teamsForBusiness",
                "attendees":[{"emailAddress":{"address":s.invitee_email},"type":"required"}],
                "transactionId":f"soast-session-{s.id}"}
            result=(await graph_request("POST",base,json=payload)).json()
            s.graph_event_id=result["id"]
        else:
            result=(await graph_request("GET",f"{base}/{s.graph_event_id}")).json()
        s.teams_url=(result.get("onlineMeeting") or {}).get("joinUrl") or s.teams_url
        if s.teams_url:
            s.state="confirmed";s.graph_error=None
        else:
            s.graph_error="Evento registrado; enlace Teams pendiente de publicación"
    except (GraphError,KeyError,ValueError,TypeError) as exc:
        s.graph_error=f"Sincronización pendiente: {str(exc) if isinstance(exc,GraphError) else type(exc).__name__}"
    db.commit()

@app.post("/api/sessions")
async def book(payload:SessionInput,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","supervisor","coordinador")
    t=scoped_ticket(db,user,payload.ticket_id)
    start,end,tech=verify_booking(db,t,payload.starts_at,payload.duration)
    booking=SupportSession(ticket_id=t.id,technician_id=tech.id,created_by=user.id,
            starts_at=start,ends_at=end,invitee_email=payload.invitee_email.strip(),state="sync_pending")
    db.add(booking);db.flush()
    for slot in slots(start,end):db.add(SessionSlot(session_id=booking.id,technician_id=tech.id,slot_at=slot))
    try:db.flush()
    except IntegrityError:db.rollback();raise HTTPException(409,"Horario reservado por otro usuario")
    audit(db,user,"book","session",str(booking.id));db.commit()
    await sync_graph(db,booking)
    return session_dict(db,booking)

@app.get("/api/sessions")
def list_sessions(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"externo","tecnico","supervisor","coordinador")
    stmt=select(SupportSession)
    if user.role=="tecnico":stmt=stmt.where(SupportSession.technician_id==user.id)
    elif user.role=="externo":stmt=stmt.join(Ticket,SupportSession.ticket_id==Ticket.id).where(Ticket.company_id==user.company_id)
    return [session_dict(db,x) for x in db.scalars(stmt.order_by(SupportSession.starts_at.desc()).limit(100)).all()]

def scoped_session(db:Session,user:User,session_id:int,modification:bool=False)->SupportSession:
    permit(user,"externo","tecnico","supervisor","coordinador")
    s=db.get(SupportSession,session_id)
    if not s:raise HTTPException(404,"Sesión no encontrada")
    t=db.get(Ticket,s.ticket_id)
    if user.role=="externo" and t.company_id!=user.company_id:raise HTTPException(404,"Sesión no encontrada")
    if user.role=="tecnico" and s.technician_id!=user.id:raise HTTPException(404,"Sesión no encontrada")
    if modification and user.role=="tecnico":raise HTTPException(403,"No puede modificar sesiones")
    return s

@app.post("/api/sessions/{session_id}/cancel")
async def cancel(session_id:int,user:User=Depends(require_user),db:Session=Depends(db)):
    s=scoped_session(db,user,session_id,True)
    if s.state=="cancelled":return session_dict(db,s)
    if utc(s.starts_at)<=now()+timedelta(minutes=15):raise HTTPException(409,"Cancelación fuera del límite")
    s.state="cancel_pending" if s.graph_event_id or (graph_configured() and s.graph_attempts>0) else "cancelled"
    if s.state=="cancelled":db.query(SessionSlot).filter(SessionSlot.session_id==s.id).delete()
    audit(db,user,"cancel","session",str(s.id));db.commit()
    # Cancellation pending retains occupied slots until Graph confirms deletion.
    if s.state=="cancel_pending":await sync_graph(db,s)
    return session_dict(db,s)

@app.post("/api/sessions/{session_id}/reconcile")
async def reconcile(session_id:int,user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"supervisor","coordinador")
    s=scoped_session(db,user,session_id)
    if s.state not in {"sync_pending","cancel_pending"}:
        raise HTTPException(409,"Sesión sin sincronización pendiente")
    await sync_graph(db,s)
    audit(db,user,"reconcile","session",str(s.id),s.state);db.commit()
    return session_dict(db,s)

@app.post("/api/sessions/{session_id}/manual-link")
def manual_link(session_id:int,payload:LinkInput,user:User=Depends(require_user),db:Session=Depends(db)):
    s=scoped_session(db,user,session_id)
    if user.role not in {"tecnico","supervisor"}:raise HTTPException(403,"Solo técnico asignado o Supervisor")
    if s.state!="sync_pending" or not s.graph_error or s.teams_url:
        raise HTTPException(409,"La contingencia requiere un error de reunión y no tener enlace")
    if not payload.teams_url.startswith("https://teams.microsoft.com/"):
        raise HTTPException(422,"Se requiere enlace seguro de Teams")
    s.teams_url=payload.teams_url;s.state="confirmed";s.graph_error=None
    audit(db,user,"manual_link","session",str(s.id));db.commit()
    return session_dict(db,s)

@app.get("/api/audit")
def audit_list(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"administrador","coordinador","supervisor")
    return [{"date":utc(a.occurred_at).isoformat(),"actor_id":a.actor_id,"action":a.action,"entity":a.entity,"entity_id":a.entity_id,"detail":a.detail} for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100)).all()]

@app.get("/api/import-issues")
def import_issues(user:User=Depends(require_user),db:Session=Depends(db)):
    permit(user,"administrador")
    return [{"source":i.source,"seq":i.source_seq,"reason":i.reason} for i in db.scalars(select(ImportIssue).order_by(ImportIssue.id.desc()).limit(100)).all()]