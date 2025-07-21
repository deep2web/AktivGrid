from flask import Flask, render_template, request, url_for, session, redirect
from authlib.integrations.flask_client import OAuth
from functools import wraps
from stravalib import Client
from pymongo import MongoClient  # MongoDB-Bibliothek importieren
# import os

app = Flask(__name__)
app.config.from_pyfile("secrets.env")
app.secret_key = "your_secret_key"  # Setze einen geheimen Schlüssel für die Session

# MongoDB-Verbindung
mongo_client = MongoClient(app.config["MONGO_URL"])  # Verbindung zur MongoDB Atlas
db = mongo_client["user_data"]  # Datenbank "aktivgrid"
users_collection = db["user_accounts"]  # Sammlung "users"

# Auth0-Konfiguration
oauth = OAuth(app)
auth0 = oauth.register(
    'auth0',
    client_id=app.config["AUTH0_CLIENT_ID"],
    client_secret=app.config["AUTH0_CLIENT_SECRET"],
    client_kwargs={
        'scope': 'openid profile email',
    },
    server_metadata_url=f'https://{app.config["AUTH0_DOMAIN"]}/.well-known/openid-configuration'
)

# Auth0 Login
@app.route("/login")
def login():
    return auth0.authorize_redirect(redirect_uri=app.config["AUTH0_CALLBACK_URL"])

# Auth0 Callback
@app.route("/callback")
def callback():
    token = auth0.authorize_access_token()
    user_info = token["userinfo"]
    session["user"] = user_info

    # Überprüfen, ob der Nutzer bereits in der Datenbank existiert
    existing_user = users_collection.find_one({"email": user_info["email"]})
    if not existing_user:
        # Neuen Nutzer in die Datenbank einfügen
        users_collection.insert_one({
            "name": user_info["name"],
            "email": user_info["email"],
            "picture": user_info["picture"],
            "created_at": user_info.get("updated_at", None)
        })
        print(f"Neuer Nutzer hinzugefügt: {user_info['email']}")

    return redirect("/dashboard")

# Auth0 Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        f'https://{app.config["AUTH0_DOMAIN"]}/v2/logout?returnTo={app.config["AUTH0_LOGOUT_URL"]}&client_id={app.config["AUTH0_CLIENT_ID"]}'
    )

# Dashboard (geschützte Route)
@app.route("/dashboard")
def dashboard():
    user = session.get("user")
    if not user:
        return redirect("/login")
    return render_template("dashboard.html", user=user)

# Geschützte Route (Decorator)
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/")
def strava_login():
    c = Client()
    url = c.authorization_url(
        client_id=app.config["STRAVA_CLIENT_ID"],
        redirect_uri=url_for("logged_in", _external=True),
        approval_prompt="auto",
    )
    return render_template("login.html", authorize_url=url)


@app.route("/strava-auth")
def logged_in():
    """
    Handles the redirect from Strava and exchanges the code for an access token.
    """
    code = request.args.get("code")
    if not code:
        return "Error: No authorization code provided by Strava.", 400

    client = Client()
    token_response = client.exchange_code_for_token(
        client_id=app.config["STRAVA_CLIENT_ID"],
        client_secret=app.config["STRAVA_CLIENT_SECRET"],
        code=code,
    )
    session["access_token"] = token_response["access_token"]
    return redirect(url_for("list_activities"))


@app.route("/activities")
@requires_auth
def list_activities():
    """
    List all activities of the authenticated athlete.
    """
    access_token = session.get("access_token")
    if not access_token:
        return "Access token is required", 400

    client = Client(access_token=access_token)
    activities = client.get_activities()

    activity_list = []
    for activity in activities:
        activity_list.append({
            "id": activity.id,
            "name": activity.name,
            "type": activity.type,
            "start_date": activity.start_date,
            "has_gps": activity.start_latlng is not None
        })

    return render_template("activities.html", activities=activity_list)

if __name__ == "__main__":
    app.run(debug=True)