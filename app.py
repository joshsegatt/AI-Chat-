from flask import Flask, send_from_directory, request, jsonify, Response
import requests, os, sqlite3, time, json

# Paths and app
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DB_PATH = os.path.join(os.path.dirname(__file__), "chat_history.db")
app = Flask(__name__)

# LM Studio config
LM_STUDIO_BASE = os.environ.get("LM_STUDIO_BASE", "http://127.0.0.1:1234/v1")
MODEL_NAME = None

# ---------- DB utilities ----------
def connect_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = connect_db()
    c = conn.cursor()
    # Sessions table
    c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT UNIQUE,
        user_id TEXT,
        title TEXT,
        created_at REAL
    )
    """)
    # Messages table
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        session_id TEXT,
        sender TEXT,
        text TEXT,
        timestamp REAL
    )
    """)
    # Indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_session ON messages(user_id, session_id)")
    conn.commit()
    conn.close()

def save_message(user_id, session_id, sender, text):
    conn = connect_db()
    c = conn.cursor()
    c.execute("INSERT INTO messages (user_id, session_id, sender, text, timestamp) VALUES (?, ?, ?, ?, ?)",
              (user_id, session_id, sender, text, time.time()))
    conn.commit()
    conn.close()

def set_session_title_if_empty(user_id, session_id, seed_text):
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT title FROM sessions WHERE user_id=? AND session_id=?", (user_id, session_id))
    row = c.fetchone()
    if row and (row[0] is None or row[0] == ""):
        title = (seed_text or "").strip()
        if len(title) > 48:
            title = title[:45].rstrip() + "..."
        c.execute("UPDATE sessions SET title=? WHERE user_id=? AND session_id=?", (title, user_id, session_id))
        conn.commit()
    conn.close()

def get_history(user_id, session_id, page=1, size=50):
    offset = (max(page,1)-1) * max(size,1)
    conn = connect_db()
    c = conn.cursor()
    c.execute("""
        SELECT sender, text FROM messages
        WHERE user_id=? AND session_id=?
        ORDER BY id ASC
        LIMIT ? OFFSET ?
    """, (user_id, session_id, size, offset))
    rows = c.fetchall()
    conn.close()
    return [{"sender": r[0], "text": r[1]} for r in rows]

def clear_history(user_id, session_id):
    conn = connect_db()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id=? AND session_id=?", (user_id, session_id))
    conn.commit()
    conn.close()

# ---------- Model detection ----------
def detect_model():
    try:
        resp = requests.get(f"{LM_STUDIO_BASE}/models", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["id"]
    except Exception as e:
        print(f"[ERRO] Não foi possível detectar modelo: {e}")
    return None

# ---------- Static ----------
@app.route("/")
def root():
    return send_from_directory(PUBLIC_DIR, "index.html")

# ---------- Sessions API ----------
@app.route("/sessions", methods=["GET", "POST"])
def sessions():
    if request.method == "POST":
        body = request.json or {}
        user_id = body.get("user_id", "default")
        title = (body.get("title") or "").strip()
        session_id = f"sess_{int(time.time()*1000)}"
        conn = connect_db()
        c = conn.cursor()
        c.execute("INSERT INTO sessions (session_id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
                  (session_id, user_id, title, time.time()))
        conn.commit()
        conn.close()
        return jsonify({"session_id": session_id, "title": title}), 201

    # GET list with pagination
    user_id = request.args.get("user_id", "default")
    page = int(request.args.get("page", "1"))
    size = int(request.args.get("size", "20"))
    offset = (max(page,1)-1) * max(size,1)
    conn = connect_db()
    c = conn.cursor()
    c.execute("""
        SELECT session_id, title, created_at
        FROM sessions WHERE user_id=?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (user_id, size, offset))
    rows = c.fetchall()
    conn.close()
    return jsonify([{"session_id": r[0], "title": r[1] or "", "created_at": r[2]} for r in rows])

@app.route("/sessions/<session_id>", methods=["PATCH", "DELETE"])
def session_detail(session_id):
    user_id = request.args.get("user_id", "default")

    if request.method == "PATCH":
        body = request.json or {}
        title = (body.get("title") or "").strip()
        conn = connect_db()
        c = conn.cursor()
        c.execute("UPDATE sessions SET title=? WHERE user_id=? AND session_id=?", (title, user_id, session_id))
        conn.commit()
        conn.close()
        return jsonify({"session_id": session_id, "title": title})

    # DELETE session (and its messages)
    conn = connect_db()
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id=? AND session_id=?", (user_id, session_id))
    c.execute("DELETE FROM sessions WHERE user_id=? AND session_id=?", (user_id, session_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ---------- History API ----------
@app.route("/history", methods=["GET", "DELETE"])
def history():
    user_id = request.args.get("user_id", "default")
    session_id = request.args.get("session_id", "default")

    if request.method == "GET":
        page = int(request.args.get("page", "1"))
        size = int(request.args.get("size", "50"))
        return jsonify(get_history(user_id, session_id, page, size))

    clear_history(user_id, session_id)
    return jsonify({"status": "ok", "message": f"Histórico limpo para {user_id}/{session_id}"})

# ---------- Completion (SSE) ----------
@app.route("/completion", methods=["POST"])
def completion():
    global MODEL_NAME
    if MODEL_NAME is None:
        MODEL_NAME = detect_model()
        if MODEL_NAME is None:
            return jsonify({"error": "Nenhum modelo disponível"}), 500

    data = request.json or {}
    user_id = data.get("user_id", "default")
    session_id = data.get("session_id", "default")
    user_prompt = (data.get("prompt") or "").strip()
    if not user_prompt:
        return jsonify({"error": "Campo 'prompt' é obrigatório"}), 400

    # Save user message
    save_message(user_id, session_id, "user", user_prompt)
    set_session_title_if_empty(user_id, session_id, user_prompt)

    system_instruction = (
        "You are a multilingual assistant. "
        "Always reply only in the same language the user uses. "
        "Never translate the message into another language. "
        "Write in a natural, human-like style — clear, concise, and conversational. "
        "Use bullet points (•) when listing ideas. "
        "Keep answers short and focused, only the essentials. "
        "Maintain a positive and motivating tone, but never exaggerated or artificial. "
        "Do not explain rules, do not give examples, do not repeat instructions."
    )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 200,
        "temperature": 0.25,
        "top_p": 0.8,
        "stream": True
    }

    def stream_sse():
        ai_text = ""
        try:
            with requests.post(
                f"{LM_STUDIO_BASE}/chat/completions",
                json=payload,
                stream=True,
                timeout=(10, 300)
            ) as r:
                for raw in r.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8")
                    if line.startswith("data: "):
                        line = line[len("data: "):]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(line)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            ai_text += delta
                            yield f"data: {json.dumps({'token': delta})}\n\n"
                    except Exception:
                        continue
        except requests.exceptions.RequestException:
            yield f"data: {json.dumps({'error': 'Model request failed'})}\n\n"

        if ai_text.strip():
            save_message(user_id, session_id, "ai", ai_text)
        yield "data: {\"done\": true}\n\n"

    return Response(stream_sse(), mimetype="text/event-stream")

# ---------- Boot ----------
if __name__ == "__main__":
    init_db()
    MODEL_NAME = detect_model()
    app.run(host="0.0.0.0", port=5000, debug=True)