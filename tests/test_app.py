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