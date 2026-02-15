from flask import Flask, jsonify, request, Response, render_template, redirect
import psycopg2
import os
import bcrypt
import random
import string
from datetime import datetime, timedelta
from functools import wraps
from supabase import create_client, Client
from psycopg2.extras import RealDictCursor


app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- DB CONNECTION ----------------

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# ---------------- ID GENERATOR ----------------

def generate_rope_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

# ---------------- AUTH ----------------

def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# ---------------- STATUS LOGIC ----------------

def compute_status(rope_id, purchase_date):
    conn = get_connection()
    cur = conn.cursor()

    # Get latest inspection
    cur.execute("""
        SELECT inspection_date, verdict
        FROM inspection_logs
        WHERE rope_id = %s
        ORDER BY inspection_date DESC
        LIMIT 1
    """, (rope_id,))

    inspection = cur.fetchone()

    if inspection:
        base_date = inspection[0]
        verdict = inspection[1]
    else:
        base_date = purchase_date
        verdict = None

    # If last inspection failed → RETIRED permanently
    if verdict == "fail":
        cur.close()
        conn.close()
        return "RETIRED"

    # Count falls since base_date
    cur.execute("""
        SELECT fall_type
        FROM fall_logs
        WHERE rope_id = %s
        AND fall_date >= %s
    """, (rope_id, base_date))

    falls = cur.fetchall()

    major = sum(1 for f in falls if f[0] == 'major')
    minor = sum(1 for f in falls if f[0] == 'minor')

    today = datetime.today().date()

    # Check fall rules
    if major >= 1 or minor >= 3:
        cur.close()
        conn.close()
        return "INSPECTION DUE"

    # Check 6 month rule
    next_due = base_date + timedelta(days=180)

    if today >= next_due:
        cur.close()
        conn.close()
        return "INSPECTION DUE"

    cur.close()
    conn.close()
    return "ACTIVE"


# ---------------- LANDING PAGE ----------------
@app.route("/")
def landing_page():
    return """
    <html>
    <head>
        <title>Rope Tracking</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #f5f5f5;
                display: flex;
                align-items: center;
                justify-content: center;
                height: 100vh;
                margin: 0;
            }
            .card {
                background: white;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 4px 15px rgba(0,0,0,0.1);
                text-align: center;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Rope Tracking System</h1>
            <p>Please scan your NFC tag to view rope details.</p>
        </div>
    </body>
    </html>
    """

# ---------------- PUBLIC ROUTE ----------------

@app.route("/rope/<rope_id>")
def rope_details(rope_id):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT rope_id, product_name, thickness, original_length,
               color, batch, manufacturing_date, purchase_date
        FROM ropes WHERE rope_id = %s
    """, (rope_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return "Rope not found", 404

    rope = {
        "rope_id": row[0],
        "product_name": row[1],
        "thickness": row[2],
        "original_length": row[3],
        "color": row[4],
        "batch": row[5],
        "manufacturing_date": row[6],
        "purchase_date": row[7],
    }

    status = compute_status(rope_id, rope["purchase_date"])

    if status == "ACTIVE":
        status_color = "green"
    elif status == "INSPECTION DUE":
        status_color = "orange"
    elif status == "RETIRED":
        status_color = "red"
    else:
        status_color = "gray"

    cur.execute("""
        SELECT image_url FROM product_variants
        WHERE product_name = %s AND color = %s
        LIMIT 1
    """, (rope["product_name"], rope["color"]))

    variant = cur.fetchone()
    image_url = variant[0] if variant else None

    cur.close()
    conn.close()

    return render_template(
        "overview.html",
        rope=rope,
        status=status,
        status_color=status_color,
        image_url=image_url
    )




@app.route("/rope/<rope_id>/inspections")
def inspection_list(rope_id):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT inspection_date,
               inspected_by,
               verdict,
               comment,
               image_url
        FROM inspection_logs
        WHERE rope_id = %s
        ORDER BY inspection_date ASC
    """, (rope_id,))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    inspections = []
    for r in rows:
        inspections.append({
            "inspection_date": r[0],
            "inspected_by": r[1],
            "verdict": r[2],
            "comment": r[3],
            "image_url": r[4]
        })

    return render_template(
        "inspections.html",
        rope_id=rope_id,
        inspections=inspections
    )


@app.route("/rope/<rope_id>/inspections/add-new", methods=["GET", "POST"])
@requires_auth
def add_inspection(rope_id):
    if request.method == "POST":

        inspection_date_str = request.form["inspection_date"]
        inspected_by = request.form["inspected_by"]
        verdict = request.form["verdict"]
        comment = request.form.get("comment")
        image = request.files.get("image")

        inspection_date = datetime.strptime(inspection_date_str, "%Y-%m-%d").date()
        today = datetime.today().date()

        if inspection_date > today:
            return "Inspection date cannot be in the future."

        image_url = None

        if image and image.filename != "":
            filename = f"{rope_id}_inspection_{datetime.now().timestamp()}.jpg"

            supabase.storage.from_("rope-media").upload(
                filename,
                image.read(),
                {"content-type": image.content_type}
            )

            image_url = supabase.storage.from_("rope-media").get_public_url(filename)

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO inspection_logs
            (rope_id, inspection_date, inspected_by, verdict, comment, image_url)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            rope_id,
            inspection_date,
            inspected_by,
            verdict,
            comment,
            image_url
        ))

        conn.commit()
        cur.close()
        conn.close()

        return redirect(f"/rope/{rope_id}/inspections")

    return render_template("add_inspection.html", rope_id=rope_id)



@app.route("/rope/<rope_id>/falls")
def fall_list(rope_id):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT fall_date, fall_time, recorded_by, fall_type, comment, image_url
        FROM fall_logs
        WHERE rope_id = %s
        ORDER BY fall_date ASC, fall_time ASC
    """, (rope_id,))

    falls = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "falls.html",
        rope_id=rope_id,
        falls=falls
    )

@app.route("/rope/<rope_id>/falls/add-new", methods=["GET", "POST"])
@requires_auth
def add_fall(rope_id):

    if request.method == "POST":

        fall_date_str = request.form["fall_date"]
        fall_time_str = request.form["fall_time"]
        recorded_by = request.form["recorded_by"]
        fall_type = request.form["fall_type"]
        comment = request.form["comment"]

        fall_date = datetime.strptime(fall_date_str, "%Y-%m-%d").date()
        today = datetime.today().date()

        # Prevent future date
        if fall_date > today:
            return render_template(
                "error.html",
                message="Fall date cannot be in the future."
            )

        image_url = None
        file = request.files.get("picture")

        if file and file.filename != "":
            file_ext = file.filename.split(".")[-1]
            file_name = f"{rope_id}_{datetime.now().timestamp()}.{file_ext}"

            file_bytes = file.read()

            supabase.storage.from_("rope-media").upload(
                file_name,
                file_bytes,
                {"content-type": file.content_type}
            )

            public_url = supabase.storage.from_("rope-media").get_public_url(file_name)
            image_url = public_url

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO fall_logs 
            (rope_id, fall_date, fall_time, recorded_by, fall_type, comment, image_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            rope_id,
            fall_date,
            fall_time_str,
            recorded_by,
            fall_type,
            comment,
            image_url
        ))

        conn.commit()
        cur.close()
        conn.close()

        return redirect(f"/rope/{rope_id}/falls")

    return render_template("add_fall.html", rope_id=rope_id)




# ---------------- ADMIN PANEL ----------------

@app.route("/admin")
@requires_auth
def admin_page():
    return render_template("admin.html")


@app.route("/admin/create", methods=["POST"])
@requires_auth
def create_rope():
    rope_id = generate_rope_id()

    password_hash = bcrypt.hashpw(
        request.form["customer_password"].encode(),
        bcrypt.gensalt()
    ).decode()

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO ropes (
            rope_id, product_name, thickness, original_length,
            color, batch, manufacturing_date, purchase_date,
            customer_password_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        rope_id,
        request.form["product_name"],
        request.form["thickness"],
        request.form["original_length"],
        request.form["color"],
        request.form["batch"],
        request.form["manufacturing_date"],
        request.form["purchase_date"],
        password_hash
    ))

    conn.commit()
    cur.close()
    conn.close()

    full_url = request.host_url.rstrip("/") + f"/rope/{rope_id}"

    return render_template(
        "rope_created.html",
        rope_id=rope_id,
        full_url=full_url
    )



# ------------- INVALID URL --------------

@app.errorhandler(404)
def page_not_found(e):
    return """
    <html>
    <body style="font-family:Arial;text-align:center;padding:50px;">
        <h2>404 - Page Not Found</h2>
        <p>The link you accessed is invalid.</p>
        <a href="/">Go to Home</a>
    </body>
    </html>
    """, 404



if __name__ == "__main__":
    app.run()


