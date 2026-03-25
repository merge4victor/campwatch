import os
import json
import sqlite3
import logging
import smtplib
import threading
from datetime import datetime

import requests
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, template_folder='../frontend/templates', static_folder='../frontend/static')
CORS(app)

DB_PATH               = os.environ.get('DB_PATH', '/data/campwatch.db')
POLL_INTERVAL_MINUTES = int(os.environ.get('POLL_INTERVAL_MINUTES', 30))
SMTP_HOST             = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT             = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER             = os.environ.get('SMTP_USER', '')
SMTP_PASS             = os.environ.get('SMTP_PASS', '')
NOTIFY_EMAIL          = os.environ.get('NOTIFY_EMAIL', '')

GDAY_API     = 'https://exp-api.gdaygroup.com.au'
GDAY_HEADERS = {
    'gday-caller': 'GdayWeb',
    'origin':      'https://embed.gdaygroup.com.au',
    'user-agent':  'Mozilla/5.0 (compatible; CampWatch/1.0)',
    'accept':      '*/*',
}

_sse_events = []
_sse_lock   = threading.Lock()


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS parks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                park_code  TEXT NOT NULL UNIQUE,
                network    TEXT NOT NULL DEFAULT 'gday',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS watches (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                park_id            INTEGER NOT NULL REFERENCES parks(id) ON DELETE CASCADE,
                accommodation_code TEXT NOT NULL,
                accommodation_name TEXT NOT NULL,
                site_subtype       TEXT NOT NULL,
                check_in           TEXT NOT NULL,
                check_out          TEXT NOT NULL,
                adults             INTEGER NOT NULL DEFAULT 2,
                children           INTEGER NOT NULL DEFAULT 0,
                muted              INTEGER NOT NULL DEFAULT 0,
                last_checked       TEXT,
                last_status        TEXT,
                last_price         REAL,
                last_count         INTEGER,
                created_at         TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                message  TEXT NOT NULL,
                price    REAL,
                count    INTEGER,
                sent_at  TEXT DEFAULT (datetime('now'))
            );
        ''')
    log.info('Database ready: %s', DB_PATH)


# ── GDay Parks API ────────────────────────────────────────────────────────────

def gday_park_info(park_code):
    r = requests.get(f'{GDAY_API}/api/v1/parks/{park_code}/info',
                     headers=GDAY_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get('result', {})

def gday_availability(park_code, check_in, check_out, adults=2, children=0):
    url = (
        f'{GDAY_API}/api/v1/parks/{park_code}/availability'
        f'?parkCode={park_code}&checkIn={check_in}&checkOut={check_out}'
        f'&adults={adults}&children={children}&infants=0'
    )
    r = requests.get(url, headers=GDAY_HEADERS, timeout=15)
    r.raise_for_status()
    result = r.json().get('result', {})
    return result.get('available', []), result.get('unavailable', [])

def extract_price(item):
    offers = item.get('offers') or []
    if offers:
        return offers[0].get('price', {}).get('totalAmount')
    return None

def check_watch(w):
    available, _ = gday_availability(
        w['park_code'], w['check_in'], w['check_out'],
        w['adults'], w['children'],
    )
    for item in available:
        if item['code'] == w['accommodation_code']:
            return True, extract_price(item), item.get('accommodationsAvailable', 1)
    return False, None, 0


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(w, price, count):
    nights = (
        datetime.strptime(w['check_out'], '%Y-%m-%d') -
        datetime.strptime(w['check_in'],  '%Y-%m-%d')
    ).days
    price_str = f'${price:.0f} total ({nights}n)' if price else 'See site'
    count_str = f'{count} site{"s" if count != 1 else ""} free'
    msg = (
        f"{w['park_name']} — {w['accommodation_name']} "
        f"| {w['check_in']} → {w['check_out']} "
        f"| {count_str} | {price_str}"
    )
    log.info('ALERT: %s', msg)

    with _sse_lock:
        _sse_events.append({
            'message': msg, 'park': w['park_name'],
            'accommodation': w['accommodation_name'],
            'check_in': w['check_in'], 'check_out': w['check_out'],
            'price': price, 'count': count, 'watch_id': w['id'],
        })
        if len(_sse_events) > 100:
            _sse_events.pop(0)

    if all([SMTP_USER, SMTP_PASS, NOTIFY_EMAIL]):
        try:
            nights_label = f'{nights} night{"s" if nights != 1 else ""}'
            subject = f'CampWatch: {w["park_name"]} available!'
            body = (
                f'CampWatch found availability!\n\n'
                f'Park:          {w["park_name"]}\n'
                f'Accommodation: {w["accommodation_name"]}\n'
                f'Check-in:      {w["check_in"]}\n'
                f'Check-out:     {w["check_out"]} ({nights_label})\n'
                f'Sites free:    {count_str}\n'
                f'Price:         {price_str}\n\n'
                f'Book: https://gdayparks.com.au\n\n'
                f'--- CampWatch {datetime.now().strftime("%d %b %Y %H:%M")} ---'
            )
            m = MIMEMultipart()
            m['From'], m['To'], m['Subject'] = SMTP_USER, NOTIFY_EMAIL, subject
            m.attach(MIMEText(body, 'plain'))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(m)
            log.info('Email sent to %s', NOTIFY_EMAIL)
        except Exception as e:
            log.error('Email failed: %s', e)


# ── Poll job ──────────────────────────────────────────────────────────────────

def poll_all():
    log.info('=== Poll start ===')
    with get_db() as conn:
        rows = conn.execute('''
            SELECT w.*, p.name AS park_name, p.park_code
            FROM watches w JOIN parks p ON p.id=w.park_id
            WHERE w.muted=0
        ''').fetchall()

    for row in rows:
        w = dict(row)
        try:
            is_avail, price, count = check_watch(w)
            status = 'available' if is_avail else 'unavailable'
            prev   = w.get('last_status')
            with get_db() as conn:
                conn.execute(
                    'UPDATE watches SET last_checked=?,last_status=?,last_price=?,last_count=? WHERE id=?',
                    (datetime.utcnow().isoformat(), status, price, count, w['id'])
                )
            if is_avail and prev != 'available':
                notify(w, price, count)
                with get_db() as conn:
                    conn.execute(
                        'INSERT INTO notifications(watch_id,message,price,count) VALUES(?,?,?,?)',
                        (w['id'],
                         f"{w['park_name']} — {w['accommodation_name']} {w['check_in']}→{w['check_out']}",
                         price, count)
                    )
            else:
                log.info('%s | %s | %s→%s | %s free',
                         status, w['accommodation_name'], w['check_in'], w['check_out'], count)
        except Exception as e:
            log.error('Watch %s error: %s', w['id'], e)
            with get_db() as conn:
                conn.execute('UPDATE watches SET last_checked=?,last_status=? WHERE id=?',
                             (datetime.utcnow().isoformat(), 'error', w['id']))
    log.info('=== Poll done ===')


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/config')
def api_config():
    return jsonify({
        'poll_interval_minutes': POLL_INTERVAL_MINUTES,
        'email_configured':      bool(SMTP_USER and SMTP_PASS and NOTIFY_EMAIL),
        'notify_email':          NOTIFY_EMAIL,
    })

@app.route('/api/parks')
def api_parks():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM parks ORDER BY name').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/parks/lookup')
def api_lookup():
    code = request.args.get('code', '').upper().strip()
    if not code:
        return jsonify({'error': 'code required'}), 400
    try:
        info = gday_park_info(code)
        name = info.get('displayName', code)
        avail, unavail = gday_availability(code, '2026-12-01', '2026-12-02')
        all_items = avail + unavail
        seen = {}
        for item in all_items:
            if item['code'] not in seen:
                seen[item['code']] = {
                    'code':               item['code'],
                    'name':               item['name'],
                    'subType':            item.get('subType', ''),
                    'subTypeDisplayName': item.get('subTypeDisplayName', item.get('subType', '')),
                }
        accommodations = sorted(seen.values(), key=lambda x: (x['subType'], x['name']))
        return jsonify({'code': code, 'name': name, 'accommodations': accommodations})
    except requests.HTTPError as e:
        code_n = e.response.status_code
        if code_n == 404:
            return jsonify({'error': f'Park code not found'}), 404
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        log.error('lookup: %s', e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/parks', methods=['POST'])
def api_add_park():
    d = request.json or {}
    if not d.get('name') or not d.get('park_code'):
        return jsonify({'error': 'name and park_code required'}), 400
    code = d['park_code'].upper()
    with get_db() as conn:
        existing = conn.execute('SELECT id FROM parks WHERE park_code=?', (code,)).fetchone()
        if existing:
            return jsonify({'id': existing['id'], 'park_code': code}), 200
        cur = conn.execute(
            'INSERT INTO parks(name,park_code,network) VALUES(?,?,?)',
            (d['name'], code, d.get('network', 'gday'))
        )
    return jsonify({'id': cur.lastrowid, 'park_code': code}), 201

@app.route('/api/parks/<int:pid>', methods=['DELETE'])
def api_delete_park(pid):
    with get_db() as conn:
        conn.execute('DELETE FROM parks WHERE id=?', (pid,))
    return jsonify({'ok': True})

@app.route('/api/watches')
def api_watches():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT w.*, p.name AS park_name, p.park_code, p.network
            FROM watches w JOIN parks p ON p.id=w.park_id
            ORDER BY w.created_at DESC
        ''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/watches', methods=['POST'])
def api_add_watch():
    d = request.json or {}
    required = ['park_id','accommodation_code','accommodation_name',
                'site_subtype','check_in','check_out']
    missing = [k for k in required if k not in d]
    if missing:
        return jsonify({'error': f'Missing: {missing}'}), 400
    with get_db() as conn:
        cur = conn.execute('''
            INSERT INTO watches(park_id,accommodation_code,accommodation_name,
                site_subtype,check_in,check_out,adults,children)
            VALUES(?,?,?,?,?,?,?,?)
        ''', (d['park_id'], d['accommodation_code'], d['accommodation_name'],
              d['site_subtype'], d['check_in'], d['check_out'],
              d.get('adults',2), d.get('children',0)))
    return jsonify({'id': cur.lastrowid}), 201

@app.route('/api/watches/<int:wid>', methods=['DELETE'])
def api_delete_watch(wid):
    with get_db() as conn:
        conn.execute('DELETE FROM watches WHERE id=?', (wid,))
    return jsonify({'ok': True})

@app.route('/api/watches/<int:wid>/mute', methods=['POST'])
def api_mute(wid):
    with get_db() as conn:
        row = conn.execute('SELECT muted FROM watches WHERE id=?', (wid,)).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        new = 0 if row['muted'] else 1
        conn.execute('UPDATE watches SET muted=? WHERE id=?', (new, wid))
    return jsonify({'muted': bool(new)})

@app.route('/api/watches/<int:wid>/check', methods=['POST'])
def api_force_check(wid):
    with get_db() as conn:
        row = conn.execute('''
            SELECT w.*, p.name AS park_name, p.park_code
            FROM watches w JOIN parks p ON p.id=w.park_id WHERE w.id=?
        ''', (wid,)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    try:
        is_avail, price, count = check_watch(dict(row))
        status = 'available' if is_avail else 'unavailable'
        with get_db() as conn:
            conn.execute(
                'UPDATE watches SET last_checked=?,last_status=?,last_price=?,last_count=? WHERE id=?',
                (datetime.utcnow().isoformat(), status, price, count, wid)
            )
        return jsonify({'status': status, 'price': price, 'count': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/notifications')
def api_notifications():
    with get_db() as conn:
        rows = conn.execute('''
            SELECT n.*, p.name AS park_name, w.accommodation_name, w.check_in, w.check_out
            FROM notifications n
            JOIN watches w ON w.id=n.watch_id
            JOIN parks   p ON p.id=w.park_id
            ORDER BY n.sent_at DESC LIMIT 50
        ''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/poll/now', methods=['POST'])
def api_poll_now():
    threading.Thread(target=poll_all, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/events')
def api_events():
    def stream():
        import time
        yield 'data: {"type":"connected"}\n\n'
        last = len(_sse_events)
        while True:
            with _sse_lock:
                cur = len(_sse_events)
            if cur > last:
                with _sse_lock:
                    new = _sse_events[last:]
                for ev in new:
                    yield f'data: {json.dumps(ev)}\n\n'
                last = cur
            time.sleep(2)
    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_all, IntervalTrigger(minutes=POLL_INTERVAL_MINUTES),
                      id='poll', replace_existing=True)
    scheduler.start()
    log.info('CampWatch running — poll every %d min', POLL_INTERVAL_MINUTES)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
