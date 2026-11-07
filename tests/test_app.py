"""Backend API integration checks using isolated SQLite database."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

file=tempfile.NamedTemporaryFile(suffix='.db',delete=False);file.close()
os.environ['SOAST_DATABASE_URL']='sqlite:///'+file.name
os.environ['SOAST_ADMIN_USERNAME']='soast_admin'
os.environ['SOAST_ADMIN_PASSWORD']='testing-password-456'

from fastapi.testclient import TestClient
from app.main import app, DbSession, Company, Ticket, TicketEvent, Shift, User, hash_password
from app.ingest import import_logs

client=TestClient(app)


def token(username,password):
    r=client.post('/api/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}


def test_bootstrap_and_roles():
    r=client.post('/api/bootstrap');assert r.status_code==200
    assert client.post('/api/bootstrap').status_code==409
    admin=token('soast_admin','testing-password-456')
    assert client.get('/api/health').status_code==200
    assert client.get('/api/tickets',headers=admin).status_code==403
    assert client.post('/api/companies',headers=admin,json={'code':'100001','name':'Test Uno'}).status_code==200
    assert client.post('/api/companies',headers=admin,json={'code':'100002','name':'Test Dos'}).status_code==200
    assert client.post('/api/users',headers=admin,json={'username':'externo1','password':'password-long-123','role':'externo','company_id':1}).status_code==200
    assert client.post('/api/users',headers=admin,json={'username':'technician1','password':'password-long-123','role':'tecnico','level':'L1'}).status_code==200
    assert client.post('/api/users',headers=admin,json={'username':'supervisor1','password':'password-long-123','role':'supervisor'}).status_code==200
    external=token('externo1','password-long-123')
    assert client.get('/api/users',headers=external).status_code==403
    assert client.get('/api/companies',headers=external).status_code==403
    assert client.get('/api/dashboard',headers=external).status_code==200
    assert client.post('/api/logout',headers=external).status_code==200
    assert client.get('/api/me',headers=external).status_code==401


def test_ingestion_and_sla(tmp_path):
    p=tmp_path/'sample.txt'
    p.write_text('\n'.join([
      '1,01/10/2026 08:00:00,,777,ALTO,100001,DAT,,actor,L1,INCIDENTE,COLA,',
      '2,01/10/2026 09:00:00,,777,ALTO,100001,DAT,technician1,actor,L1,INCIDENTE,ESPERA,',
      '3,01/10/2026 12:00:00,,777,ALTO,100001,DAT,technician1,actor,L1,INCIDENTE,PROGRESO,SEG',
      '4,01/10/2026 13:00:00,,777,ALTO,100001,DAT,technician1,actor,L1,INCIDENTE,RESUELTO,',
      '5,01/10/2026 08:00:00,,777,ALTO,100002,DAT,,actor,L1,INCIDENTE,COLA,',
      '6,01/10/2026 08:00:00,,778,ALTO,999999,DAT,,actor,L1,INCIDENTE,COLA,',
    ]))
    result=import_logs(p);assert result['accepted']==5;assert result['invalid']==1
    assert import_logs(p)['duplicate']==5
    supervisor=token('supervisor1','password-long-123')
    tickets=client.get('/api/tickets?q=777',headers=supervisor).json();assert len(tickets)==2
    t=next(t for t in tickets if t['company_id']==1)
    assert t['product']=='DATOS';assert t['sla']['hours']==2
    assert t['sla']['state']=='cumplido'
    assert client.get('/api/tickets/'+str(t['id']),headers=supervisor).json()['events'][2]['status']=='PROGRESO'
    external=token('externo1','password-long-123')
    own=client.get('/api/tickets?q=777',headers=external).json();assert len(own)==1
    other=next(t for t in tickets if t['company_id']==2)
    assert client.get('/api/tickets/'+str(other['id']),headers=external).status_code==404
    assert client.get('/api/import-issues',headers=external).status_code==403


def test_session_permission_and_collision():
    # Select a future weekday in local timezone for deterministic availability.
    from zoneinfo import ZoneInfo
    local=datetime.now(ZoneInfo('America/Guatemala'))+timedelta(days=5)
    while local.weekday()>4: local+=timedelta(days=1)
    supervisor=token('supervisor1','password-long-123')
    with DbSession() as db:
        tech=db.query(User).filter_by(username='technician1').one()
        techid=tech.id
    assert client.post('/api/shifts',headers=supervisor,json={'user_id':techid,'weekday':local.weekday(),'start_minute':480,'end_minute':1080}).status_code==200
    with DbSession() as db:
        t=Ticket(company_id=1,legacy_id='500',type='INCIDENTE',priority='MEDIO',product='VOIP',status='COLA',level='L1',first_seen=datetime.now(timezone.utc),last_seen=datetime.now(timezone.utc))
        db.add(t);db.commit();tid=t.id
    available=client.get('/api/availability',params={'ticket_id':tid,'day':local.date().isoformat(),'duration':30},headers=supervisor)
    assert available.status_code==200
    assert available.json()['starts_at']
    selected=available.json()['starts_at'][0]
    external=token('externo1','password-long-123')
    p={'ticket_id':tid,'starts_at':selected,'duration':30,'invitee_email':'test@example.org'}
    with TestClient(app) as c:
        r=c.post('/api/sessions',headers=external,json=p)
    assert r.status_code==200,r.text
    assert r.json()['state']=='sync_pending'
    with TestClient(app) as c:
        clash=c.post('/api/sessions',headers=external,json=p)
    assert clash.status_code==409
    assert client.get('/api/sessions',headers=token('technician1','password-long-123')).status_code==200


def test_export_and_account_reset():
    admin=token('soast_admin','testing-password-456')
    external=token('externo1','password-long-123')
    result=client.get('/api/reports/tickets.csv',headers=external)
    assert result.status_code==200
    assert 'soast-tickets.csv' in result.headers['content-disposition']
    assert b'2,777' not in result.content  # No cross-company CSV data.
    assert b'1,777' in result.content
    assert client.get('/api/escalations',headers=external).status_code==200
    assert client.get('/api/reports/tickets.csv',headers=admin).status_code==403
    with DbSession() as db:
        target=db.query(User).filter_by(username='externo1').one()
    r=client.post(f'/api/users/{target.id}/password',headers=admin,json={'password':'another-safe-password'})
    assert r.status_code==200
    assert client.get('/api/me',headers=external).status_code==401
    assert client.post('/api/login',json={'username':'externo1','password':'password-long-123'}).status_code==401
    assert token('externo1','another-safe-password')


def test_graph_certificate_auth_and_safe_retries(monkeypatch, tmp_path):
    import asyncio
    import sys
    from types import SimpleNamespace
    import httpx
    from app import graph
    from app.main import SupportSession, sync_graph

    cert=tmp_path/'graph.crt';key=tmp_path/'graph.key'
    cert.write_text('TEST-CERT');key.write_text('TEST-KEY')
    monkeypatch.setenv('SOAST_GRAPH_TENANT_ID','test-tenant')
    monkeypatch.setenv('SOAST_GRAPH_CLIENT_ID','test-client')
    monkeypatch.setenv('SOAST_GRAPH_ORGANIZER','organizer@example.org')
    monkeypatch.setenv('SOAST_GRAPH_CERT_PATH',str(cert))
    monkeypatch.setenv('SOAST_GRAPH_KEY_PATH',str(key))
    monkeypatch.delenv('SOAST_GRAPH_CLIENT_SECRET',raising=False)

    observed={}
    class App:
        def __init__(self,**kwargs):observed.update(kwargs)
        def acquire_token_for_client(self,**kwargs):return {'access_token':'TEST-TOKEN'}
    monkeypatch.setitem(sys.modules,'msal',SimpleNamespace(ConfidentialClientApplication=App))
    graph._msal_app.cache_clear()
    assert graph.graph_configured()
    assert graph._get_token()=='TEST-TOKEN'
    assert observed['client_credential']['private_key']=='TEST-KEY'
    assert observed['client_credential']['public_certificate']=='TEST-CERT'

    with DbSession() as db:
        ticket=db.query(Ticket).filter_by(legacy_id='500').one()
        tech=db.query(User).filter_by(username='technician1').one()
        supervisor=db.query(User).filter_by(username='supervisor1').one()
        date=datetime.now(timezone.utc)+timedelta(days=8)
        session=SupportSession(ticket_id=ticket.id,technician_id=tech.id,created_by=supervisor.id,
                               starts_at=date,ends_at=date+timedelta(minutes=30),
                               invitee_email='client@example.org',state='sync_pending')
        db.add(session);db.commit()
        sid=session.id

    calls=[]
    async def fake(method,path,**kwargs):
        calls.append((method,path,kwargs))
        if method=='POST' and path.endswith('/cancel'):
            return httpx.Response(202)
        if method=='POST':
            if len([c for c in calls if c[0]=='POST' and not c[1].endswith('/cancel')])==1:
                raise graph.GraphError('Microsoft Graph HTTP 503')
            return httpx.Response(201,json={'id':'event-123',
                'onlineMeeting':{'joinUrl':'https://teams.microsoft.com/l/meetup-join/example'}})
        raise AssertionError((method,path))
    monkeypatch.setattr('app.main.graph_request',fake)
    with DbSession() as db:
        session=db.get(SupportSession,sid)
        asyncio.run(sync_graph(db,session))
        assert session.state=='sync_pending' and session.graph_attempts==1
        asyncio.run(sync_graph(db,session))
        assert session.state=='confirmed' and session.graph_event_id=='event-123'
        assert session.teams_url.startswith('https://teams.microsoft.com/')
        session.state='cancel_pending';db.commit()
        asyncio.run(sync_graph(db,session))
        assert session.state=='cancelled' and session.graph_error is None
    assert '/cancel' in calls[-1][1]
    assert calls[1][2]['json']['transactionId']==f'soast-session-{sid}'

def test_dated_shift_import_is_atomic_dated_and_idempotent(tmp_path):
    from zoneinfo import ZoneInfo
    import pytest
    from sqlalchemy import select, func
    from app.ingest import import_dated_shifts
    from app.main import DatedShift, ScheduleImportCoverage, candidates

    path = tmp_path / "sample_roster.csv"
    path.write_text(
        "usuario,nivel,tipo,fecha,hora_inicio,hora_fin\\n"
        "technician1,L1,TURNO1,2026-10-01,08:00,10:00\\n"
        "typo_tech,L1,TURNO2_20-24,2026-10-02,20:00,23:59\\n",
        encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Usuarios sin correspondencia"):
        import_dated_shifts(path, dry_run=True)
    with DbSession() as db:
        assert db.scalar(select(func.count()).select_from(DatedShift)) == 0
        assert db.scalar(select(func.count()).select_from(ScheduleImportCoverage)) == 0
    mapping = {"typo_tech": "technician1"}
    dry = import_dated_shifts(path, aliases=mapping, dry_run=True)
    assert dry["would_create"] == 2 and dry["dry_run"]
    with DbSession() as db:
        assert db.scalar(select(func.count()).select_from(DatedShift)) == 0
    imported = import_dated_shifts(path, aliases=mapping)
    assert imported["created"] == 2 and imported["duplicate"] == 0
    rerun = import_dated_shifts(path, aliases=mapping)
    assert rerun["created"] == 0 and rerun["duplicate"] == 2
    supervisor = token("supervisor1", "password-long-123")
    external = token("externo1", "another-safe-password")
    tech = token("technician1", "password-long-123")
    months = client.get("/api/schedule-months", headers=supervisor).json()["months"]
    assert "2026-10" in months
    rows = client.get("/api/dated-shifts?month=2026-10", headers=supervisor).json()
    assert len(rows) == 2
    assert {row["kind"] for row in rows} == {"TURNO1", "TURNO2_20-24"}
    assert client.get("/api/dated-shifts?month=2026-10", headers=tech).status_code == 200
    assert client.get("/api/dated-shifts?month=2026-10", headers=external).status_code == 403
    assert client.get("/api/dated-shifts?month=2026-99", headers=supervisor).status_code == 422
    with DbSession() as db:
        ticket = db.query(Ticket).filter_by(legacy_id="500").one()
        at = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("America/Guatemala"))
        assert candidates(db, ticket, at, at+timedelta(minutes=30))
        outside = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("America/Guatemala"))
        assert candidates(db, ticket, outside, outside+timedelta(minutes=30)) == []


def test_guard_roster_is_not_bookable(tmp_path):
    from zoneinfo import ZoneInfo
    from app.ingest import import_dated_shifts
    from app.main import candidates
    admin = token("soast_admin", "testing-password-456")
    r = client.post("/api/users", headers=admin, json={
        "username": "technician_guard", "password": "password-long-123",
        "role": "tecnico", "level": "L3"
    })
    assert r.status_code == 200, r.text
    path = tmp_path/"guard.csv"
    path.write_text(
        "usuario,nivel,tipo,fecha,hora_inicio,hora_fin\\n"
        "technician_guard,L3,GUARDIA_L3,2026-10-03,00:00,23:59\\n",
        encoding="utf-8"
    )
    assert import_dated_shifts(path)["created"] == 1
    with DbSession() as db:
        ticket=db.query(Ticket).filter_by(legacy_id="500").one()
        ticket.level = "L3"
        db.commit()
        at=datetime(2026,10,3,12,0,tzinfo=ZoneInfo("America/Guatemala"))
        assert candidates(db,ticket,at,at+timedelta(minutes=30)) == []
