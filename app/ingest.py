"""Safe, idempotent CLI importer for TEECMO inputs.

Usage: python -m app.ingest companies DatosTeecmo.xlsx
       python -m app.ingest technicians DatosTeecmo.xlsx
       python -m app.ingest logs 0901261.txt
       python -m app.ingest shifts turnos_agentes_2026-10.csv [--dry-run] [--alias ORIGEN=USUARIO]
Never commit source customer files or this import's local database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .main import Company, DatedShift, DbSession, ImportIssue, ScheduleImportCoverage, Ticket, TicketEvent, User, hash_password

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



SHIFT_KINDS = {"TURNO1", "TURNO2_00-06", "TURNO2_20-24", "GUARDIA_L3"}
SHIFT_COLUMNS = ["usuario", "nivel", "tipo", "fecha", "hora_inicio", "hora_fin"]


def _minute(value: str) -> int:
    try:
        hour, minute = value.split(":")
        if len(hour) != 2 or len(minute) != 2 or not hour.isdecimal() or not minute.isdecimal():
            raise ValueError
        number = int(hour) * 60 + int(minute)
        if int(hour) > 23 or int(minute) > 59:
            raise ValueError
        return number
    except (ValueError, AttributeError) as exc:
        raise ValueError("hora inválida (se requiere HH:MM)") from exc


def import_dated_shifts(path: Path, aliases: dict[str, str] | None = None, dry_run: bool = False):
    """Atomic import of actual dated schedules; invalid users do not become new accounts.

    A source spelling difference must be resolved explicitly with --alias, not guessed.
    Schedule coverage is committed only with valid rows, so days off remain unbookable.
    """
    aliases = {key.strip().lower(): value.strip().lower() for key, value in (aliases or {}).items()}
    parsed = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != SHIFT_COLUMNS:
            raise ValueError("Encabezados inválidos: se requieren " + ",".join(SHIFT_COLUMNS))
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(row.get(field) is None for field in SHIFT_COLUMNS):
                    raise ValueError("número de columnas inválido")
                source_user = row["usuario"].strip().lower()
                username = aliases.get(source_user, source_user)
                level = row["nivel"].strip().upper()
                kind = row["tipo"].strip().upper()
                day = date.fromisoformat(row["fecha"].strip())
                start = _minute(row["hora_inicio"].strip())
                end = _minute(row["hora_fin"].strip())
                if not username or level not in {"L1", "L2", "L3"} or kind not in SHIFT_KINDS:
                    raise ValueError("usuario, nivel o tipo desconocido")
                if kind == "GUARDIA_L3" and level != "L3":
                    raise ValueError("guardia requiere nivel L3")
                if not 0 <= start < end <= 1439:
                    raise ValueError("rango horario inválido")
                parsed.append((line, source_user, username, level, kind, day, start, end))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"Línea {line}: {exc}") from exc
    if not parsed:
        raise ValueError("El CSV no contiene jornadas para importar")
    start_day = min(item[5] for item in parsed)
    end_day = max(item[5] for item in parsed)
    source = path.name
    with DbSession() as db:
        users = {u.username.lower(): u for u in db.scalars(select(User).where(User.role == "tecnico")).all()}
        missing = {}
        mismatched = {}
        for _, source_user, username, level, _, _, _, _ in parsed:
            user = users.get(username)
            if user is None:
                missing[source_user] = missing.get(source_user, 0) + 1
            elif user.level != level:
                mismatched[source_user] = f"{user.level} != {level}"
        if missing or mismatched:
            problems = []
            if missing:
                problems.append("Usuarios sin correspondencia: " + ", ".join(
                    f"{key} ({count} jornadas)" for key, count in sorted(missing.items())))
            if mismatched:
                problems.append("Niveles diferentes: " + ", ".join(
                    f"{key} ({value})" for key, value in sorted(mismatched.items())))
            raise ValueError("; ".join(problems) + ". No se importó ningún turno. Revise el origen o use --alias origen=usuario_confirmado.")
        existing = {
            (s.user_id, s.work_date, s.start_minute, s.end_minute, s.kind)
            for s in db.scalars(select(DatedShift).where(
                DatedShift.work_date >= start_day, DatedShift.work_date <= end_day)).all()
        }
        unique = set()
        additions = []
        duplicates = 0
        for line, _, username, _, kind, day, start, end in parsed:
            key = (users[username].id, day, start, end, kind)
            if key in existing or key in unique:
                duplicates += 1
            else:
                unique.add(key)
                additions.append((line, key))
        result = {
            "total": len(parsed), "created" if not dry_run else "would_create": len(additions),
            "duplicate": duplicates, "from": start_day.isoformat(), "to": end_day.isoformat(),
            "dry_run": dry_run
        }
        if dry_run:
            return result
        for line, (user_id, day, start, end, kind) in additions:
            db.add(DatedShift(user_id=user_id, work_date=day, start_minute=start,
                              end_minute=end, kind=kind, source=source, source_line=line))
        if not db.scalar(select(ScheduleImportCoverage.id).where(
                ScheduleImportCoverage.source == source,
                ScheduleImportCoverage.start_day == start_day,
                ScheduleImportCoverage.end_day == end_day).limit(1)):
            db.add(ScheduleImportCoverage(source=source, start_day=start_day, end_day=end_day))
        db.commit()
        return result

def main():
    parser=argparse.ArgumentParser(description="Importación local segura de fuentes TEECMO")
    parser.add_argument("mode",choices=["companies","technicians","logs","shifts"])
    parser.add_argument("path",type=Path)
    parser.add_argument("--alias", action="append", default=[], metavar="ORIGEN=USUARIO",
                        help="Asociación explícita para nombres diferentes; no modifica cuentas")
    parser.add_argument("--dry-run", action="store_true", help="Validar sin escribir en la BD")
    args=parser.parse_args()
    if not args.path.is_file():parser.error("Archivo no encontrado")
    if args.mode != "shifts" and (args.alias or args.dry_run):
        parser.error("--alias y --dry-run solo se admiten para shifts")
    if args.mode == "shifts":
        aliases = {}
        for mapping in args.alias:
            if mapping.count("=") != 1 or not all(mapping.split("=")):
                parser.error("--alias requiere ORIGEN=USUARIO")
            source_name, target_name = mapping.split("=", 1)
            aliases[source_name] = target_name
        try:
            print(import_dated_shifts(args.path, aliases=aliases, dry_run=args.dry_run))
        except ValueError as exc:
            parser.error(str(exc))
        return
    operation={"companies":import_companies,"technicians":import_technicians,"logs":import_logs}[args.mode]
    print(operation(args.path))

if __name__=="__main__":main()