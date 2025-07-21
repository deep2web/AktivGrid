from flask import Flask, render_template, request, url_for, session
from stravalib import Client

app = Flask(__name__)
app.config.from_pyfile("secrets.env")
app.secret_key = "your_secret_key"  # Setze einen geheimen Schlüssel für die Session


@app.route("/")
def login():
    c = Client()
    url = c.authorization_url(
        client_id=app.config["STRAVA_CLIENT_ID"],
        redirect_uri=url_for(".logged_in", _external=True),
        approval_prompt="auto",
    )
    return render_template("login.html", authorize_url=url)


@app.route("/strava-oauth")
def logged_in():
    """
    Method called by Strava (redirect) that includes parameters.
    - state
    - code
    - error
    """
    error = request.args.get("error")
    if error:
        return render_template("login_error.html", error=error)
    else:
        code = request.args.get("code")
        client = Client()
        token_response = client.exchange_code_for_token(
            client_id=app.config["STRAVA_CLIENT_ID"],
            client_secret=app.config["STRAVA_CLIENT_SECRET"],
            code=code,
        )
        # Extrahiere den tatsächlichen Access Token
        access_token = token_response["access_token"]
        session["access_token"] = access_token  # Speichere nur den Access Token in der Session
        print(f"Access Token: {access_token}")

        strava_athlete = client.get_athlete()
        return render_template(
            "login_results.html",
            athlete=strava_athlete,
            access_token=access_token,
        )


@app.route("/activities")
def list_activities():
    """
    List all activities of the authenticated athlete.
    """
    # Hole den Access Token aus der Session
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
            "has_gps": activity.start_latlng is not None  # Prüfe, ob GPS-Daten vorhanden sind
        })

    return render_template("activities.html", activities=activity_list)


if __name__ == "__main__":
    app.run(debug=True)