"""Safe, idempotent CLI importer for TEECMO inputs.

Usage: python -m app.ingest companies DatosTeecmo.xlsx
       python -m app.ingest technicians DatosTeecmo.xlsx
       python -m app.ingest logs 0901261.txt
Never commit source customer files or this import's local database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .main import Company, DbSession, ImportIssue, Ticket, TicketEvent, User, hash_password

NORMAL_STATUS = {"DIAGNOSTICO":"DIAGNOSTICO", "DIAGNÓSTICO":"DIAGNOSTICO","COLA":"COLA", "PROGRESO":"PROGRESO", "ESPERA":"ESPERA", "RESUELTO":"RESUELTO", "CERRADO":"CERRADO"}
LEVELS = {"L1","L2","L3","FABRICA"}
KINDS = {"SOLICITUD","INCIDENTE","INCIDENT"}


def import_companies(path:Path):
    from openpyxl import load_workbook
    wb=load_workbook(path,read_only=True,data_only=True)
    ws=wb["Empresas"]
    created=0
    with DbSession() as db:
        for values in ws.iter_rows(min_row=2,values_only=True):
            code=str(values[0]).strip() if values[0] is not None else ""
            name=str(values[1]).strip() if values[1] else ""
            if not code or not name:continue
            if not db.scalar(select(Company).where(Company.code==code)):
                db.add(Company(code=code,name=name));created+=1
        db.commit()
    return {"companies_created":created}


def import_technicians(path:Path):
    from openpyxl import load_workbook
    wb=load_workbook(path,read_only=True,data_only=True)
    created=0
    with DbSession() as db:
        for values in wb["Usuarios Teecmo"].iter_rows(min_row=2,values_only=True):
            username=str(values[1]).strip().lower() if values[1] else ""
            level=str(values[3]).strip().upper() if values[3] else ""
            if not username or level not in {"L1","L2","L3"}:continue
            if not db.scalar(select(User).where(User.username==username)):
                # Disabled until administrator sets a private password and activates.
                db.add(User(username=username,role="tecnico",level=level,email=str(values[2]) if values[2] else None,
                            password_hash=hash_password(secrets.token_urlsafe(36)),active=False));created+=1
        db.commit()
    return {"technicians_created_disabled":created}


def _parse_line(line:str, source:str, number:int):
    values=next(csv.reader([line]))
    if len(values)!=13:raise ValueError(f"se esperaban 13 columnas, se recibieron {len(values)}")
    sequence, date_text, related, ticket_id, priority, company_code, product, owner, actor, level, kind, state, activity=[s.strip() for s in values]
    if not sequence.isdigit() or not ticket_id or not company_code:raise ValueError("identificador, secuencia o empresa faltante")
    try:at=datetime.strptime(date_text,"%d/%m/%Y %H:%M:%S").replace(tzinfo=ZoneInfo("America/Guatemala")).astimezone(timezone.utc)
    except ValueError as exc:raise ValueError("fecha inválida") from exc
    state=state.upper().replace("Ó","O")
    if state not in NORMAL_STATUS or level.upper() not in LEVELS or kind.upper() not in KINDS:
        raise ValueError("estado, nivel o tipo desconocido")
    if priority.upper() not in {"ALTO","MEDIO","BAJO"}:raise ValueError("prioridad inválida")
    product={"DAT":"DATOS"}.get(product.upper(),product.upper())
    if product not in {"VOIP","VIDEO","SMS","VPN","POS","CLOUD","DATOS","CHAT","GPS"}:
        raise ValueError("producto desconocido")
    return {"seq":sequence,"timestamp":at,"related":related,"ticket":ticket_id,"priority":priority.upper(),
            "company_code":company_code,"product":product,"owner":owner or None,"actor":actor or None,
            "level":level.upper(),"kind":"INCIDENTE" if kind.upper()=="INCIDENT" else kind.upper(),
            "status":NORMAL_STATUS[state],"activity":{"DIAG":"DIG"}.get(activity.upper(),activity.upper()) or None}


def import_logs(path:Path):
    # Idempotence is scoped to the source's stable name and sequence.
    source=path.name
    metrics={"accepted":0,"duplicate":0,"invalid":0,"tickets":0}
    changed=set()
    with DbSession() as db:
        with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as fd:
            for number,line in enumerate(fd,1):
                if not line.strip():continue
                try:
                    data=_parse_line(line,source,number)
                    seq=data["seq"]
                except (ValueError,csv.Error) as exc:
                    seq=f"line-{number}"
                    if not db.scalar(select(ImportIssue).where(ImportIssue.source==source,ImportIssue.source_seq==seq)):
                        db.add(ImportIssue(source=source,source_seq=seq,reason=str(exc)[:500]))
                    metrics["invalid"]+=1
                    continue
                if db.scalar(select(TicketEvent.id).where(TicketEvent.source==source,TicketEvent.source_seq==seq).limit(1)):
                    metrics["duplicate"]+=1;continue
                company=db.scalar(select(Company).where(Company.code==data["company_code"]))
                if not company:
                    if not db.scalar(select(ImportIssue).where(ImportIssue.source==source,ImportIssue.source_seq==seq)):
                        db.add(ImportIssue(source=source,source_seq=seq,reason="Empresa sin correspondencia: "+data["company_code"]))
                    metrics["invalid"]+=1;continue
                ticket=db.scalar(select(Ticket).where(Ticket.company_id==company.id,Ticket.legacy_id==data["ticket"]))
                if not ticket:
                    ticket=Ticket(company_id=company.id,legacy_id=data["ticket"],type=data["kind"],priority=data["priority"],
                                  product=data["product"],status=data["status"],level=data["level"],owner=data["owner"],
                                  first_seen=data["timestamp"],last_seen=data["timestamp"],complete_history=False)
                    db.add(ticket);db.flush();metrics["tickets"]+=1
                event=TicketEvent(ticket_id=ticket.id,source=source,source_seq=seq,occurred_at=data["timestamp"],
                                  status=data["status"],owner=data["owner"],level=data["level"],actor=data["actor"],activity=data["activity"])
                db.add(event);changed.add(ticket.id)
                # Flush so duplicate sequences inside the same input are detected.
                db.flush();metrics["accepted"]+=1
        for ticket_id in changed:
            t=db.get(Ticket,ticket_id)
            ordered=db.scalars(select(TicketEvent).where(TicketEvent.ticket_id==ticket_id).order_by(TicketEvent.occurred_at,TicketEvent.id)).all()
            first,last=ordered[0],ordered[-1]
            t.first_seen=first.occurred_at;t.last_seen=last.occurred_at
            t.complete_history=first.status=="COLA"
            t.status=last.status;t.level=last.level;t.owner=last.owner
        db.commit()
    return metrics


def main():
    parser=argparse.ArgumentParser(description="Importación local segura de fuentes TEECMO")
    parser.add_argument("mode",choices=["companies","technicians","logs"])
    parser.add_argument("path",type=Path)
    args=parser.parse_args()
    if not args.path.is_file():parser.error("Archivo no encontrado")
    operation={"companies":import_companies,"technicians":import_technicians,"logs":import_logs}[args.mode]
    print(operation(args.path))

if __name__=="__main__":main()