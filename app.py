import asyncio
import os
import threading
from flask import Flask, request, render_template, make_response, jsonify, send_from_directory, send_file

from ReplyOptimizer.db_functions.users_functions import get_thread
from db_functions.setup import init_db
from db_functions.users_functions import get_session, add_user, update_user, get_inbox
from worker.worker import SessionManager

app = Flask(__name__)
manager = SessionManager()


def get_session_log_content(session: str, lines: int = 100) -> str:
    if not session:
        return "Session ID not given"

    filename = f"logs/session_{session[:8]}.log"

    if not os.path.exists(filename):
        return f"Log for session {session[:8]}... still not created."

    try:
        with open(filename, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            last_lines = all_lines[-lines:]
            return "".join(last_lines)
    except Exception as e:
        return f"Error reading log: {str(e)}"

@app.route('/', methods=['GET'])
def index():
    session = request.cookies.get('session_id')
    if session:
        user = get_session(session)
        if not user:
            rendered = render_template('auth.html', message="Your previous session has expired")

            resp = make_response(rendered)
            resp.delete_cookie('session_id')

            return resp
        else:
            return render_template("dashboard.html")
    else:
        return render_template("auth.html")

@app.route('/newUser', methods=['POST'])
def newUser():
    try:
        data = request.get_json()
        required_fields = ['gemini_key', 'datadog_key', 'datadog_site', 'imap_host', 'smtp_host', "instructions"]

        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({
                'success': False,
                'error': f'Missing fields: {", ".join(missing_fields)}'
            }), 400

        gemini_key = data['gemini_key']
        datadog_api_key = data['datadog_key']
        datadog_site = data['datadog_site']
        imap_server = data['imap_host']
        smtp_server = data['smtp_host']
        instructions = data['instructions']

        session_id = add_user(gemini_key, datadog_api_key, datadog_site, imap_server, smtp_server, instructions)

        if session_id:
            response = make_response(jsonify({'success': True}))
            response.set_cookie(
                'session_id',
                session_id,
                max_age=60 * 60 * 24 * 365 * 2,
                path="/",
                samesite="Lax"
            )
            return response
        else:
            return jsonify({'success': False, 'error': 'Failed to add user'}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/settings/update', methods=['post'])
def updateSettings():
    session = request.cookies.get('session_id')
    data = request.get_json()

    if session:
        gemini_key = data['gemini_key']
        datadog_api_key = data['datadog_api_key']
        datadog_site = data['datadog_site']
        imap_server = data['primary_host']
        smtp_server = data['primary_smtp_host']
        imap_accounts = data['imap_keys']
        instructions = data['instructions']

        success = update_user(session, gemini_key, datadog_api_key, datadog_site, imap_server, smtp_server, instructions, imap_accounts)
        if not success:
            return jsonify({'success': False})

        return jsonify({'success': True})
    else:
        return jsonify({'success': False})

@app.route('/settings', methods=['GET'])
def get_settings():
    session = request.cookies.get('session_id')
    if not session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    user_data = get_session(session)
    if not user_data:
        return jsonify({'success': False, 'error': 'Session not found'}), 404

    response_data = {
        'success': True,
        'gemini_key': user_data.get('gemini_key', ''),
        'datadog_api_key': user_data.get('datadog_api_key', ''),
        'datadog_site': user_data.get('datadog_site', ''),
        'imap_primary_host': user_data.get('imap_primary_host', ''),
        'smtp_primary_host': user_data.get('smtp_primary_host', ''),
        'instructions': user_data.get('instructions', ''),
        'imap_accounts': [
            {
                'email': acc['mail'],
                'password': acc['password'],
                'host': acc['imap_host'].split(':')[0] if ':' in acc['imap_host'] else acc['imap_host'],
                'port': acc['imap_host'].split(':')[1] if ':' in acc['imap_host'] else '993',
                'smtp_host': acc['smtp_host'].split(':')[0] if ':' in acc['smtp_host'] else acc['smtp_host'],
                'smtp_port': acc['smtp_host'].split(':')[1] if ':' in acc['smtp_host'] else '465',
            }
            for acc in user_data.get('imap_accounts', [])
        ]
    }

    return jsonify(response_data)

@app.route('/inbox', methods=['GET'])
def inbox_handler():
    session = request.cookies.get('session_id')
    if session:
        inbox = get_inbox(session)
        if inbox:
            return inbox
        else:
            return jsonify({'success': False})
    else:
        return jsonify({'message': 'User unauthorized'}), 401

@app.route('/thread/<thread_id>', methods=['GET'])
def thread_handler(thread_id):
    session = request.cookies.get('session_id')
    if session:
        messages = get_thread(session, thread_id)
        if messages:
            return messages
        else:
            return jsonify({'success': False})
    else:
        return jsonify({'message': 'User unauthorized'}), 401

@app.route('/logs/get', methods=['GET'])
def get_logs():
    session = request.cookies.get('session_id')
    if session:
        logs = get_session_log_content(session)
        return jsonify({'success': True, "logs": logs})
    else:
        return jsonify({'success': False})

@app.route('/logs/download', methods=['GET'])
def download_logs():
    session = request.cookies.get('session_id')
    if not session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    filename = f"logs/session_{session[:8]}.log"
    download_name = f"reply_optimizer.log"

    if not os.path.exists(filename):
        return "Log not found", 404

    return send_file(
        filename,
        as_attachment=True,
        download_name=download_name,
        mimetype='text/plain'
    )

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory('templates', filename)

async def stop_session(session: str):
    worker = manager.sessions.get(session)
    if worker:
        await worker.stop()
        manager.sessions.pop(session)

async def manager_loop():
    while True:
        await manager.sync_sessions()
        await asyncio.sleep(30)

if __name__ == '__main__':
    init_db()

    threading.Thread(target=lambda: asyncio.run(manager_loop()), daemon=True).start()

    app.run(host="0.0.0.0", port=5004, debug=True)