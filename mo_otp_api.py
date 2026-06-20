from flask import Flask, request, jsonify
from flask_cors import CORS
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import string
import time
import hashlib
import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
import logging
from collections import defaultdict
import threading
import os

# ===== إعدادات التسجيل =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mo_otp.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== إنشاء التطبيق =====
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

# ===== إعدادات البريد =====
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = "merufizogabu84@gmail.com"
SMTP_PASSWORD = "hkwj tcgr lknx yqgc"
FROM_EMAIL = "merufizogabu84@gmail.com"
FROM_NAME = "Mo Otp API"

# ===== إعدادات الحدود =====
RATE_LIMIT_PER_MINUTE = 30

# ===== قاعدة البيانات =====
DB_PATH = "mo_otp.db"

# ===== تخزين مؤقت =====
rate_limit_store = defaultdict(lambda: {'count': 0, 'reset_time': time.time() + 60})
rate_limit_lock = threading.Lock()

# ===== دوال قاعدة البيانات =====

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS otp_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expiry INTEGER NOT NULL,
                max_attempts INTEGER DEFAULT 3,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP,
                metadata TEXT,
                request_id TEXT UNIQUE,
                status TEXT DEFAULT 'active'
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                action TEXT,
                status TEXT,
                ip TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        logger.info("✅ Database initialized")

def generate_request_id():
    return f"otp_{int(time.time())}_{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"

def generate_code(length: int = 6) -> str:
    return ''.join(random.choices(string.digits, k=length))

def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()

def check_rate_limit(email: str) -> dict:
    with rate_limit_lock:
        current_time = time.time()
        user_data = rate_limit_store[email]
        
        if current_time > user_data['reset_time']:
            user_data['count'] = 0
            user_data['reset_time'] = current_time + 60
        
        if user_data['count'] >= RATE_LIMIT_PER_MINUTE:
            time_left = int(user_data['reset_time'] - current_time)
            return {
                'allowed': False,
                'limit': RATE_LIMIT_PER_MINUTE,
                'remaining': 0,
                'reset_in': time_left,
                'used': user_data['count']
            }
        
        user_data['count'] += 1
        
        return {
            'allowed': True,
            'limit': RATE_LIMIT_PER_MINUTE,
            'remaining': RATE_LIMIT_PER_MINUTE - user_data['count'],
            'reset_in': int(user_data['reset_time'] - current_time),
            'used': user_data['count']
        }

def log_action(email: str, action: str, status: str, ip: str = None, user_agent: str = None):
    with get_db() as conn:
        conn.execute(
            'INSERT INTO api_logs (email, action, status, ip, user_agent) VALUES (?, ?, ?, ?, ?)',
            (email, action, status, ip, user_agent)
        )
        conn.commit()

def send_email(to_email: str, subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"✅ Email sent to {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Email error: {e}")
        return False

# ===== نقاط النهاية =====

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "service": "Mo Otp API",
        "version": "1.0.0",
        "status": "online",
        "email": FROM_EMAIL,
        "rate_limit": f"{RATE_LIMIT_PER_MINUTE} طلب في الدقيقة"
    })

@app.route('/send-otp', methods=['POST'])
def send_otp():
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "بيانات غير صالحة"}), 400
        
        email = data.get('email')
        subject = data.get('subject', '🔐 كود التحقق - Mo Otp')
        message = data.get('message', 'مرحباً!\nكود التحقق الخاص بك هو: {code}\nالصلاحية: 5 دقائق')
        expiry_seconds = data.get('expiry_seconds', 300)
        code_length = data.get('code_length', 6)
        store_code = data.get('store_code', True)
        max_attempts = data.get('max_attempts', 3)
        
        if not email:
            return jsonify({"error": "البريد الإلكتروني مطلوب"}), 400
        
        rate_check = check_rate_limit(email)
        
        if not rate_check['allowed']:
            log_action(email, "send_otp", "rate_limited", request.remote_addr, request.headers.get('User-Agent'))
            return jsonify({
                "success": False,
                "message": f"تجاوزت حد الطلبات المسموح ({RATE_LIMIT_PER_MINUTE} طلب في الدقيقة)",
                "rate_limit": rate_check
            }), 429
        
        code = generate_code(code_length)
        request_id = generate_request_id()
        message_body = message.format(code=code)
        
        if store_code:
            with get_db() as conn:
                conn.execute(
                    '''INSERT INTO otp_codes 
                       (email, code, expiry, max_attempts, request_id) 
                       VALUES (?, ?, ?, ?, ?)''',
                    (
                        email,
                        hash_code(code),
                        int(time.time()) + expiry_seconds,
                        max_attempts,
                        request_id
                    )
                )
                conn.commit()
        
        if send_email(email, subject, message_body):
            log_action(email, "send_otp", "success", request.remote_addr, request.headers.get('User-Agent'))
            return jsonify({
                "success": True,
                "message": "تم إرسال كود التحقق بنجاح",
                "data": {
                    "email": email,
                    "code": code if store_code else None,
                    "expiry_seconds": expiry_seconds
                },
                "request_id": request_id,
                "rate_limit": {
                    "limit": rate_check['limit'],
                    "remaining": rate_check['remaining'],
                    "reset_in": rate_check['reset_in']
                }
            })
        else:
            log_action(email, "send_otp", "failed", request.remote_addr, request.headers.get('User-Agent'))
            return jsonify({"success": False, "message": "فشل في إرسال البريد"}), 500
            
    except Exception as e:
        logger.error(f"Error in send_otp: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "بيانات غير صالحة"}), 400
        
        email = data.get('email')
        code = data.get('code')
        request_id = data.get('request_id')
        
        if not email or not code:
            return jsonify({"error": "البريد الإلكتروني والكود مطلوبان"}), 400
        
        with get_db() as conn:
            query = 'SELECT * FROM otp_codes WHERE email = ? AND status = "active"'
            params = [email]
            
            if request_id:
                query += ' AND request_id = ?'
                params.append(request_id)
            
            query += ' ORDER BY created_at DESC LIMIT 1'
            
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            
            if not row:
                log_action(email, "verify_otp", "not_found", request.remote_addr, request.headers.get('User-Agent'))
                return jsonify({
                    "success": False,
                    "message": "لا يوجد كود نشط",
                    "is_valid": False
                })
            
            current_time = int(time.time())
            if current_time > row['expiry']:
                conn.execute(
                    'UPDATE otp_codes SET status = "expired" WHERE id = ?',
                    (row['id'],)
                )
                conn.commit()
                log_action(email, "verify_otp", "expired", request.remote_addr, request.headers.get('User-Agent'))
                return jsonify({
                    "success": False,
                    "message": "انتهت صلاحية الكود",
                    "is_valid": False
                })
            
            if row['attempts'] >= row['max_attempts']:
                conn.execute(
                    'UPDATE otp_codes SET status = "blocked" WHERE id = ?',
                    (row['id'],)
                )
                conn.commit()
                log_action(email, "verify_otp", "max_attempts", request.remote_addr, request.headers.get('User-Agent'))
                return jsonify({
                    "success": False,
                    "message": "تجاوزت عدد المحاولات المسموح",
                    "is_valid": False
                })
            
            hashed_input = hash_code(code)
            if hashed_input != row['code']:
                conn.execute(
                    'UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?',
                    (row['id'],)
                )
                conn.commit()
                
                remaining = row['max_attempts'] - row['attempts'] - 1
                log_action(email, "verify_otp", "invalid", request.remote_addr, request.headers.get('User-Agent'))
                return jsonify({
                    "success": False,
                    "message": f"كود غير صحيح، تبقى {remaining} محاولات",
                    "is_valid": False,
                    "remaining_attempts": remaining
                })
            
            conn.execute(
                '''UPDATE otp_codes 
                   SET status = "verified", verified_at = ? 
                   WHERE id = ?''',
                (datetime.now().isoformat(), row['id'])
            )
            conn.commit()
            
            log_action(email, "verify_otp", "success", request.remote_addr, request.headers.get('User-Agent'))
            return jsonify({
                "success": True,
                "message": "تم التحقق بنجاح",
                "is_valid": True,
                "data": {
                    "email": email,
                    "verified_at": datetime.now().isoformat()
                }
            })
            
    except Exception as e:
        logger.error(f"Error in verify_otp: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/otp-status/<email>', methods=['GET'])
def otp_status(email):
    try:
        with get_db() as conn:
            cursor = conn.execute(
                'SELECT * FROM otp_codes WHERE email = ? AND status = "active" ORDER BY created_at DESC LIMIT 1',
                (email,)
            )
            row = cursor.fetchone()
            
            if not row:
                return jsonify({
                    "success": False,
                    "exists": False,
                    "expired": False
                })
            
            current_time = int(time.time())
            is_expired = current_time > row['expiry']
            
            if is_expired:
                conn.execute(
                    'UPDATE otp_codes SET status = "expired" WHERE id = ?',
                    (row['id'],)
                )
                conn.commit()
            
            return jsonify({
                "success": True,
                "exists": True,
                "expired": is_expired,
                "data": {
                    "email": row['email'],
                    "time_left": max(0, row['expiry'] - current_time),
                    "attempts": row['attempts'],
                    "max_attempts": row['max_attempts'],
                    "request_id": row['request_id']
                }
            })
            
    except Exception as e:
        logger.error(f"Error in otp_status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/rate-limit/<email>', methods=['GET'])
def get_rate_limit(email):
    try:
        current_time = time.time()
        user_data = rate_limit_store.get(email)
        
        if not user_data:
            return jsonify({
                "email": email,
                "limit": RATE_LIMIT_PER_MINUTE,
                "used": 0,
                "remaining": RATE_LIMIT_PER_MINUTE,
                "reset_in": 60
            })
        
        time_left = int(user_data['reset_time'] - current_time)
        
        return jsonify({
            "email": email,
            "limit": RATE_LIMIT_PER_MINUTE,
            "used": user_data['count'],
            "remaining": max(0, RATE_LIMIT_PER_MINUTE - user_data['count']),
            "reset_in": max(0, time_left)
        })
        
    except Exception as e:
        logger.error(f"Error in get_rate_limit: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        with get_db() as conn:
            total = conn.execute('SELECT COUNT(*) FROM otp_codes').fetchone()[0]
            active = conn.execute('SELECT COUNT(*) FROM otp_codes WHERE status = "active"').fetchone()[0]
            verified = conn.execute('SELECT COUNT(*) FROM otp_codes WHERE status = "verified"').fetchone()[0]
            expired = conn.execute('SELECT COUNT(*) FROM otp_codes WHERE status = "expired"').fetchone()[0]
            
            return jsonify({
                "total_otp": total,
                "active": active,
                "verified": verified,
                "expired": expired,
                "rate_limit": {
                    "per_minute": RATE_LIMIT_PER_MINUTE,
                    "active_users": len(rate_limit_store)
                }
            })
            
    except Exception as e:
        logger.error(f"Error in get_stats: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/logs', methods=['GET'])
def get_logs():
    try:
        limit = request.args.get('limit', 100, type=int)
        
        with get_db() as conn:
            cursor = conn.execute(
                'SELECT * FROM api_logs ORDER BY created_at DESC LIMIT ?',
                (limit,)
            )
            logs = [dict(row) for row in cursor.fetchall()]
            return jsonify({"logs": logs})
            
    except Exception as e:
        logger.error(f"Error in get_logs: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/clear-otp/<email>', methods=['DELETE'])
def clear_otp(email):
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE otp_codes SET status = "cancelled" WHERE email = ? AND status = "active"',
                (email,)
            )
            deleted = conn.total_changes
            conn.commit()
            
            log_action(email, "clear_otp", "success")
            return jsonify({
                "success": True,
                "message": f"تم حذف {deleted} كود",
                "deleted": deleted
            })
            
    except Exception as e:
        logger.error(f"Error in clear_otp: {e}")
        return jsonify({"error": str(e)}), 500

init_db()

def cleanup_rate_limits():
    while True:
        time.sleep(120)
        current_time = time.time()
        with rate_limit_lock:
            expired = []
            for email, data in rate_limit_store.items():
                if current_time > data['reset_time']:
                    expired.append(email)
            for email in expired:
                del rate_limit_store[email]

threading.Thread(target=cleanup_rate_limits, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
