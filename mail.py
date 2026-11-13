#!/usr/bin/env python3
"""Automatische E-Mails der DC Rot-Blau Vereins-App.

Läuft per GitHub Actions im privaten Daten-Repository, liest kasse.json und
verschickt über das GMX-Postfach:
  - Hinweise auf neue Termine (an alle mit E-Mail-Zustimmung)
  - Erinnerungen an alle, die für einen anstehenden Termin noch nicht geantwortet haben
  - eine Warnung an alle, wenn kurz vor einem Spiel zu wenige Zusagen da sind
  - eine Erinnerung an den Admin, bevor der Token abläuft
Was schon verschickt wurde, steht in mail-state.json, damit nichts doppelt kommt.
"""
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
USER = os.environ.get("GMX_USER", "").strip()
PASS = os.environ.get("GMX_PASS", "")
APP = os.environ.get("APP_URL", "").strip()
ADMIN = os.environ.get("ADMIN_MAIL", "").strip() or USER
TEST = os.environ.get("TEST_MAIL", "").lower() == "true"

ET = {"heim": "Heimspiel", "auswaerts": "Auswärtsspiel", "training": "Training",
      "sitzung": "Teamsitzung", "sonst": "Sonstiges"}
WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


D = load("kasse.json", {})
S = load("mail-state.json", {})
for key in ("newEv", "remind", "short", "token"):
    S.setdefault(key, {})

settings = D.get("settings") or {}
admin = D.get("admin") or {}
club = settings.get("club") or "DC Rot-Blau Wuppertal"
cfg = {"newEv": True, "remind": True, "days": 2, "short": True, "token": True}
cfg.update(admin.get("mail") or {})
p_min = int(float(settings.get("playersMin") or 6))
p_ok = max(p_min, int(float(settings.get("playersOk") or 8)))

deleted = set(D.get("deleted") or [])
members = [m for m in D.get("members") or [] if not m.get("blocked") and m.get("id") not in deleted]
recipients = [m for m in members if m.get("mailOk") and m.get("email")]
events = [e for e in D.get("events") or [] if e.get("id") not in deleted and e.get("date")]
rsvps = {r["id"]: r for r in D.get("rsvps") or [] if r.get("id") not in deleted}
today = dt.datetime.now(TZ).date()

FOOT = ("\n\n--\nDiese E-Mail kommt automatisch aus der Vereins-App des " + club + ".\n"
        "Du bekommst sie, weil du in deinem Profil E-Mails aktiviert hast. Abbestellen: "
        "in der App unter Einstellungen den Haken entfernen.")
if APP:
    FOOT += "\n" + APP

smtp = None
sent = 0


def connect():
    global smtp
    if smtp is None:
        smtp = smtplib.SMTP("mail.gmx.net", 587, timeout=30)
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(USER, PASS)
    return smtp


def send(addresses, subject, body, hidden=True):
    """Schickt eine E-Mail. hidden=True: alle Empfänger als Blindkopie."""
    global sent
    addresses = sorted({a.strip() for a in addresses if a and a.strip()})
    if not addresses:
        return
    for i in range(0, len(addresses), 25):
        chunk = addresses[i:i + 25]
        msg = EmailMessage()
        msg["From"] = formataddr((club, USER))
        msg["To"] = formataddr((club, USER)) if hidden else ", ".join(chunk)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="dc-rotblau-app")
        msg.set_content(body + FOOT)
        connect().send_message(msg, from_addr=USER, to_addrs=chunk)
        sent += len(chunk)
    print(f"Gesendet: {subject} an {len(addresses)} Empfänger")


def day_text(d):
    return f"{WD[d.weekday()]}, {d:%d.%m.%Y}"


def event_text(e):
    d = dt.date.fromisoformat(e["date"])
    lines = [f"{ET.get(e.get('type'), 'Termin')}: {e.get('title', '')}",
             day_text(d) + (f", {e['time']} Uhr" if e.get("time") else "")]
    if e.get("place"):
        lines.append("Ort: " + e["place"])
    if e.get("meet"):
        lines.append("Treffpunkt: " + e["meet"])
    if e.get("note"):
        lines += ["", e["note"]]
    return "\n".join(lines)


def answer(e, m):
    return (rsvps.get(f"{e['id']}~{m['id']}") or {}).get("status")


def app_hint():
    return f"\n\nBitte in der App zu- oder absagen: {APP}" if APP else "\n\nBitte in der App zu- oder absagen."


def main():
    if not USER or not PASS:
        sys.exit("GMX_USER oder GMX_PASS fehlt. Bitte unter Settings > Secrets and variables > Actions anlegen.")

    if TEST:
        send([ADMIN], f"Test: E-Mails der {club}-App funktionieren",
             "Wenn du diese E-Mail liest, ist der Versand über GMX richtig eingerichtet.\n\n"
             f"Derzeit haben {len(recipients)} Mitglieder E-Mails aktiviert.", hidden=False)
        return

    # Beim allerersten Lauf keine alten Termine nachmelden
    if not S.get("init"):
        for e in events:
            S["newEv"][e["id"]] = today.isoformat()
        S["init"] = today.isoformat()
        print("Erster Lauf: vorhandene Termine als bekannt markiert.")

    upcoming = sorted([e for e in events if dt.date.fromisoformat(e["date"]) >= today],
                      key=lambda e: (e["date"], e.get("time") or ""))

    # 1. Neue Termine
    new = [e for e in upcoming if e["id"] not in S["newEv"]]
    if new and cfg.get("newEv"):
        if len(new) == 1:
            subject = f"Neuer Termin: {ET.get(new[0].get('type'), 'Termin')} {new[0].get('title', '')}, {dt.date.fromisoformat(new[0]['date']):%d.%m.}"
        else:
            subject = f"{len(new)} neue Termine beim {club}"
        body = "Hallo zusammen,\n\nin der Vereins-App " + ("ist ein neuer Termin" if len(new) == 1 else "sind neue Termine") + " eingetragen:\n\n"
        body += "\n\n".join(event_text(e) for e in new) + app_hint()
        send([m["email"] for m in recipients], subject, body)
    for e in new:
        S["newEv"][e["id"]] = today.isoformat()

    for e in upcoming:
        days = (dt.date.fromisoformat(e["date"]) - today).days
        if days > int(cfg.get("days") or 2):
            continue
        # 2. Persönliche Erinnerung an alle ohne Antwort
        if cfg.get("remind"):
            for m in recipients:
                key = f"{e['id']}~{m['id']}"
                if not answer(e, m) and key not in S["remind"]:
                    when = "heute" if days == 0 else "morgen" if days == 1 else f"in {days} Tagen"
                    send([m["email"]], f"Bitte antworten: {ET.get(e.get('type'), 'Termin')} {e.get('title', '')} {when}",
                         f"Hallo {m.get('name', '')},\n\nfür diesen Termin fehlt noch deine Antwort:\n\n{event_text(e)}{app_hint()}",
                         hidden=False)
                    S["remind"][key] = today.isoformat()
        # 3. Zu wenige Zusagen kurz vor einem Spiel
        if cfg.get("short") and e.get("type") in ("heim", "auswaerts") and e["id"] not in S["short"]:
            yes = sum(1 for m in members if answer(e, m) == "ja")
            if yes < p_min:
                send([m["email"] for m in recipients],
                     f"Zu wenige Spieler: {e.get('title', '')} am {dt.date.fromisoformat(e['date']):%d.%m.}",
                     f"Hallo zusammen,\n\nfür dieses Spiel haben bisher nur {yes} zugesagt. "
                     f"Wir brauchen mindestens {p_min}, besser {p_ok}.\n\n{event_text(e)}\n\n"
                     "Wer kann, sagt bitte in der App zu." + (f"\n{APP}" if APP else ""))
                S["short"][e["id"]] = today.isoformat()

    # 4. Token läuft ab (nur an den Admin)
    exp = admin.get("tokenExpiry")
    if cfg.get("token") and exp:
        left = (dt.date.fromisoformat(exp) - today).days
        due = [t for t in (14, 7, 3, 1, 0) if left <= t and f"{exp}:{t}" not in S["token"]]
        if due:
            send([ADMIN], f"Token der {club}-App läuft {'heute' if left <= 0 else f'in {left} Tagen'} ab",
                 f"Der Zugangs-Token läuft am {dt.date.fromisoformat(exp):%d.%m.%Y} ab.\n\n"
                 "So geht's: bei GitHub unter Settings > Developer settings > Personal access tokens > "
                 "Fine-grained tokens den Token öffnen, Regenerate token wählen, das neue Ablaufdatum in der "
                 "App unter Verwaltung eintragen und den neuen Token an die Mitglieder schicken.", hidden=False)
            for t in due:
                S["token"][f"{exp}:{t}"] = today.isoformat()

    # Alte Einträge nach 90 Tagen aufräumen
    cutoff = (today - dt.timedelta(days=90)).isoformat()
    for key in ("newEv", "remind", "short", "token"):
        S[key] = {k: v for k, v in S[key].items() if v >= cutoff}


try:
    main()
except smtplib.SMTPAuthenticationError:
    sys.exit("GMX hat die Anmeldung abgelehnt. Prüfe GMX_USER und GMX_PASS und ob im GMX-Postfach unter "
             "Einstellungen > POP3/IMAP Abruf der Zugriff für externe Programme erlaubt ist.")
finally:
    if smtp is not None:
        try:
            smtp.quit()
        except Exception:
            pass

if not TEST:
    with open("mail-state.json", "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, indent=1)
print(f"Fertig. {sent} E-Mail(s) verschickt.")
