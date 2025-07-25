from flask import Flask, render_template, request, url_for, session, redirect, jsonify
from authlib.integrations.flask_client import OAuth
from functools import wraps
from stravalib import Client
from pymongo import MongoClient  # MongoDB-Bibliothek importieren
from bson.objectid import ObjectId
from datetime import datetime, timedelta
import uuid


app = Flask(__name__)
app.config.from_pyfile("secrets.env")
app.secret_key = "your_secret_key"  # Setze einen geheimen Schlüssel für die Session

# MongoDB-Verbindung
mongo_client = MongoClient(app.config["MONGO_URL"])  # Verbindung zur MongoDB Atlas
db = mongo_client["user_data"]  # Datenbank "aktivgrid"
users_collection = db["user_accounts"]  # Sammlung "users"
# Collection für Benutzeraktivitäten
user_activities_collection = db["user_activities"]
# Collection für Strava-Accounts (unter den bestehenden Collections)
strava_accounts_collection = db["strava_accounts"]
# Collection für versteckte Aktivitäten
hidden_activities_collection = db["hidden_activities"]

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

    # Nach Login direkt zu den Aktivitäten weiterleiten statt zum Dashboard
    return redirect("/activities")

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
def home():
    """
    Zeigt die Hauptseite mit Login-Button an.
    """
    return render_template("index.html")

# Strava-Login auf eine andere Route verschieben (falls noch benötigt)
@app.route("/strava")
def strava_login():
    c = Client()
    url = c.authorization_url(
        client_id=app.config["STRAVA_CLIENT_ID"],
        redirect_uri=url_for("logged_in", _external=True),
        approval_prompt="auto",
    )
    return render_template("login.html", authorize_url=url)


@app.route("/strava-auth")
@requires_auth
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
    
    # Benutzerinformationen abrufen
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    user_id = user_obj["_id"]
    
    # Tokendaten in MongoDB speichern
    strava_account = {
        "user_id": user_id,
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "expires_at": token_response["expires_at"],
        "created_at": datetime.now().replace(tzinfo=None)
    }
    
    # Prüfen, ob Benutzer bereits einen Strava-Account hat
    existing_strava_account = strava_accounts_collection.find_one({"user_id": user_id})
    if existing_strava_account:
        strava_accounts_collection.update_one({"user_id": user_id}, {"$set": strava_account})
    else:
        strava_accounts_collection.insert_one(strava_account)
    
    # Access Token für diese Sitzung in Session speichern
    session["access_token"] = token_response["access_token"]
    
    return redirect(url_for("list_activities"))


@app.route("/activities")
@requires_auth
def list_activities():
    """
    List all activities of the authenticated athlete, optionally filtered by date range.
    """
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    user_id = user_obj["_id"]

    # Prüfen, ob der Benutzer die Strava-Verbindung übersprungen hat
    skipped = request.args.get('skipped') == 'true'

    # Datumsbereich aus der Anfrage abrufen
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    # Kombinierte Liste für alle Aktivitäten
    all_activities = []

    # Liste der versteckten Aktivitäten abrufen
    hidden_activities = list(hidden_activities_collection.find({"user_id": user_id}))
    hidden_ids = {(h["activity_id"], h["source"]) for h in hidden_activities}

    # Manuelle Aktivitäten aus der DB laden
    query = {"user_id": user_id}
    if start_date:
        query["start_date"] = {"$gte": datetime.strptime(start_date, "%Y-%m-%d")}
    if end_date:
        if "start_date" in query:
            query["start_date"]["$lte"] = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            query["start_date"] = {"$lte": datetime.strptime(end_date, "%Y-%m-%d")}

    manual_activities = list(user_activities_collection.find(query))

    for activity in manual_activities:
        # Konvertiere start_date in naive datetime
        start_date = activity["start_date"]
        if start_date.tzinfo is not None:
            start_date = start_date.replace(tzinfo=None)

        # Aktivitäts-ID als String speichern
        activity_id = str(activity["_id"])
        
        # Prüfen, ob die Aktivität versteckt ist
        is_hidden = (activity_id, "manual") in hidden_ids

        all_activities.append({
            "id": activity_id,
            "name": activity["name"],
            "type": activity["type"],
            "start_date": start_date,
            "has_gps": False,
            "source": "manual",
            "hidden": is_hidden
        })

    # Prüfen, ob der Nutzer mit Strava verbunden ist
    strava_account = strava_accounts_collection.find_one({"user_id": user_id})
    strava_connected = strava_account is not None

    # Access-Token aus Datenbank abrufen oder bei Bedarf aktualisieren
    access_token = None
    if strava_connected:
        access_token = refresh_strava_token(user_id)
        # Token auch in der Session speichern
        session["access_token"] = access_token

    # Strava-Aktivitäten laden, falls ein Token vorhanden ist
    if access_token:
        client = Client(access_token=access_token)
        try:
            strava_activities = client.get_activities()

            for activity in strava_activities:
                # Konvertiere start_date in naive datetime
                activity_date = activity.start_date
                if activity_date.tzinfo is not None:
                    activity_date = activity_date.replace(tzinfo=None)

                # Filtere Strava-Aktivitäten nach Datumsbereich
                if request.args.get("start_date") and request.args.get("end_date"):
                    filter_start = datetime.strptime(request.args.get("start_date"), "%Y-%m-%d")
                    filter_end = datetime.strptime(request.args.get("end_date"), "%Y-%m-%d")
                    # Füge einen Tag zu filter_end hinzu, damit auch Aktivitäten des Enddatums eingeschlossen werden
                    filter_end = filter_end.replace(hour=23, minute=59, second=59)
                    
                    if activity_date < filter_start or activity_date > filter_end:
                        continue

                # Aktivitäts-ID als String speichern
                activity_id = str(activity.id)
                
                # Prüfen, ob die Aktivität versteckt ist
                is_hidden = (activity_id, "strava") in hidden_ids

                all_activities.append({
                    "id": activity_id,
                    "name": activity.name,
                    "type": activity.type,
                    "start_date": activity_date,
                    "has_gps": activity.start_latlng is not None,
                    "source": "strava",
                    "hidden": is_hidden
                })
        except Exception as e:
            print(f"Fehler beim Abrufen der Strava-Aktivitäten: {e}")

    # Aktivitäten nach Datum sortieren (neueste zuerst)
    all_activities.sort(key=lambda x: x["start_date"], reverse=True)

    # Nur zur Strava-Verbindungsseite leiten, wenn keine Aktivitäten UND kein Token UND nicht übersprungen
    if not all_activities and not strava_connected and not skipped:
        # Falls keine Aktivitäten und kein Strava-Token
        return render_template("connect_strava.html")

    # WICHTIG: Immer frische Benutzerdaten aus der Datenbank laden
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    
    # Aktualisiere auch die Session mit den frischen Daten
    session["user"]["name"] = user_obj.get("name", user["name"])
    session.modified = True

    # Am Ende der Funktion die frischen Benutzerdaten aus der Datenbank verwenden
    return render_template(
        "activities.html", 
        activities=all_activities, 
        strava_connected=strava_connected,
        user={
            "name": user_obj.get("name", user["name"]),
            "email": user["email"],
            "picture": user["picture"]
        }  # Aktuelle Benutzerdaten aus DB
    )

@app.route("/add-activity", methods=["POST"])
@requires_auth
def add_activity():
    """
    Fügt eine manuell eingegebene Aktivität zur Datenbank hinzu
    """
    user = session.get("user")
    user_id = users_collection.find_one({"email": user["email"]})["_id"]
    
    # Formularfelder auslesen
    name = request.form.get("name", "Unbenannte Aktivität")
    activity_type = request.form.get("type", "other")
    start_date = request.form.get("start_date")
    distance_str = request.form.get("distance", "")
    duration_str = request.form.get("duration", "")
    description = request.form.get("description", "")
    
    # Sicherstellen, dass numerische Werte korrekt verarbeitet werden
    try:
        distance = float(distance_str) if distance_str.strip() else 0
    except (ValueError, AttributeError):
        distance = 0
    
    try:
        duration = float(duration_str) if duration_str.strip() else 0
    except (ValueError, AttributeError):
        duration = 0
    
    # Datetime konvertieren und Zeitzoneninformationen entfernen
    try:
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%dT%H:%M").replace(tzinfo=None)
    except (ValueError, TypeError):
        start_date_obj = datetime.now().replace(tzinfo=None)  # Bei ungültigem Datum aktuelles Datum verwenden
    
    # Aktivitäten speichern: Einzeln oder wöchentlich wiederholt
    repeat_weekly = request.form.get("repeat_weekly") == "on"
    repeat_end_str = request.form.get("repeat_end_date")
    repeat_end = None
    if repeat_end_str:
        try:
            repeat_end = datetime.strptime(repeat_end_str, "%Y-%m-%dT%H:%M").replace(tzinfo=None)
        except ValueError:
            repeat_end = None

    base = {
        "user_id": user_id,
        "name": name,
        "type": activity_type,
        "distance": distance,
        "duration": duration,
        "description": description,
        "source": "manual",
        "created_at": datetime.now().replace(tzinfo=None)
    }

    if not repeat_weekly:
        base.update({"start_date": start_date_obj, "repeating": False})
        user_activities_collection.insert_one(base)
    else:
        group = str(uuid.uuid4())
        date = start_date_obj
        end = repeat_end or (start_date_obj + timedelta(weeks=52))
        while date <= end:
            entry = base.copy()
            entry.update({"start_date": date, "repeating": True, "repeat_group_id": group})
            user_activities_collection.insert_one(entry)
            date += timedelta(weeks=1)

    return redirect("/activities")

def refresh_strava_token(user_id):
    """
    Prüft den Strava-Token und aktualisiert ihn, wenn er abgelaufen ist
    """
    strava_account = strava_accounts_collection.find_one({"user_id": user_id})
    
    if not strava_account:
        return None
    
    # Prüfen, ob der Token abgelaufen ist
    current_time = int(datetime.now().timestamp())
    if current_time < strava_account["expires_at"]:
        # Token ist noch gültig
        return strava_account["access_token"]
    
    # Token ist abgelaufen, erneuern
    try:
        refresh_response = Client().refresh_access_token(
            client_id=app.config["STRAVA_CLIENT_ID"],
            client_secret=app.config["STRAVA_CLIENT_SECRET"],
            refresh_token=strava_account["refresh_token"]
        )
        
        # Token-Informationen in MongoDB aktualisieren
        strava_accounts_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "access_token": refresh_response["access_token"],
                "refresh_token": refresh_response["refresh_token"],
                "expires_at": refresh_response["expires_at"]
            }}
        )
        
        return refresh_response["access_token"]
    except Exception as e:
        print(f"Fehler beim Aktualisieren des Tokens: {e}")
        return None

@app.route("/disconnect-strava", methods=["POST"])
@requires_auth
def disconnect_strava():
    """
    Trennt den Strava-Account vom Nutzer und löscht den Eintrag in der MongoDB.
    """
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    user_id = user_obj["_id"]

    # Strava-Account aus der MongoDB löschen
    strava_accounts_collection.delete_one({"user_id": user_id})

    # Access-Token aus der Session entfernen
    session.pop("access_token", None)

    print(f"Strava-Account für Nutzer {user['email']} wurde getrennt.")
    return redirect("/activities")

@app.route("/update-settings", methods=["POST"])
@requires_auth
def update_settings():
    """
    Aktualisiert die Benutzereinstellungen in der Datenbank
    """
    user = session.get("user")
    new_name = request.form.get("name")
    
    print(f"Einstellungen aktualisieren - Alter Name: {user['name']}, Neuer Name: {new_name}")
    
    # Aktualisiere den Namen in der Datenbank
    if new_name and new_name != user["name"]:
        result = users_collection.update_one(
            {"email": user["email"]},
            {"$set": {"name": new_name}}
        )
        
        # Überprüfe, ob die Aktualisierung erfolgreich war
        if result.modified_count == 1:
            print(f"Name für Nutzer {user['email']} wurde zu '{new_name}' in der Datenbank geändert.")
            
            # Aktualisiere die Session (komplett neu laden aus der DB)
            user_obj = users_collection.find_one({"email": user["email"]})
            session["user"] = {
                "name": user_obj.get("name"),
                "email": user["email"],
                "picture": user["picture"]
            }
            session.modified = True  # Wichtig: Markiert die Session als geändert
            
            print(f"Session aktualisiert. Neuer Name in Session: {session['user']['name']}")
        else:
            print(f"Fehler: Name konnte nicht in der Datenbank aktualisiert werden!")
    
    # Cache-Busting-Parameter hinzufügen
    timestamp = datetime.now().timestamp()
    return redirect(f"/activities?_={timestamp}")

@app.route("/toggle-activity-visibility", methods=["POST"])
@requires_auth
def toggle_activity_visibility():
    """
    Versteckt oder zeigt eine Aktivität an
    """
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    user_id = user_obj["_id"]
    
    activity_id = request.form.get("activity_id")
    source = request.form.get("source")  # "strava" oder "manual"
    
    # Prüfen, ob die Aktivität bereits versteckt ist
    existing_hidden = hidden_activities_collection.find_one({
        "user_id": user_id,
        "activity_id": activity_id,
        "source": source
    })
    
    if existing_hidden:
        # Aktivität wieder anzeigen
        hidden_activities_collection.delete_one({
            "user_id": user_id,
            "activity_id": activity_id,
            "source": source
        })
        action = "visible"
    else:
        # Aktivität verstecken
        hidden_activities_collection.insert_one({
            "user_id": user_id,
            "activity_id": activity_id,
            "source": source,
            "hidden_at": datetime.now().replace(tzinfo=None)
        })
        action = "hidden"
    
    return jsonify({"status": "success", "action": action})

@app.route("/delete-activity", methods=["POST"])
@requires_auth
def delete_activity():
    """
    Löscht eine manuell erstellte Aktivität aus der Datenbank
    """
    user = session.get("user")
    user_obj = users_collection.find_one({"email": user["email"]})
    user_id = user_obj["_id"]
    
    activity_id = request.form.get("activity_id")
    
    try:
        # Sicherstellen, dass die Aktivität dem aktuellen Benutzer gehört und manuell ist
        activity = user_activities_collection.find_one({
            "_id": ObjectId(activity_id),
            "user_id": user_id,
            "source": "manual"
        })
        
        if not activity:
            return jsonify({"status": "error", "message": "Aktivität nicht gefunden oder keine Berechtigung"}), 404
        
        # Aktivität aus der Datenbank löschen
        result = user_activities_collection.delete_one({"_id": ObjectId(activity_id)})
        
        if result.deleted_count == 1:
            # Auch eventuell vorhandene Einträge in hidden_activities löschen
            hidden_activities_collection.delete_one({
                "user_id": user_id,
                "activity_id": activity_id,
                "source": "manual"
            })
            
            return jsonify({"status": "success", "message": "Aktivität gelöscht"})
        else:
            return jsonify({"status": "error", "message": "Aktivität konnte nicht gelöscht werden"}), 500
        
    except Exception as e:
        print(f"Fehler beim Löschen der Aktivität: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
