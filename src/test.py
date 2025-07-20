from stravalib import Client
from datetime import datetime
from dotenv import load_dotenv
import os


# Lade die Umgebungsvariablen aus der .env-Datei
load_dotenv("src/test.env")

# Hole den Access Token aus der Umgebungsvariable
access_token = os.getenv("ACCESS_TOKEN")


client = Client(access_token)
print(client.get_athlete())  # Get current athlete details

# Definiere den Zeitraum
after_date = datetime(2024, 7, 1)  # Aktivitäten nach dem 1. Januar 2023
before_date = datetime(2025, 7, 31)  # Aktivitäten vor dem 31. Dezember 2023

# Hole die Aktivitäten im definierten Zeitraum
activities = client.get_activities(after=after_date, before=before_date)

# Filtere Aktivitäten nach Typ (z. B. nur "Ride")
filtered_activities = [activity for activity in activities if activity.type == "Run"]

# Gib die IDs und andere Details der Aktivitäten aus
for activity in filtered_activities:
    print(f"ID: {activity.id}, Name: {activity.name}, Typ: {activity.type}, Datum: {activity.start_date}")