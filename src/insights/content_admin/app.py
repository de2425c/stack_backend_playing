"""Flask admin UI for managing poker content.

Supports both Firestore (production) and local JSON (development).
Set USE_LOCAL_DB=1 to use local JSON file instead of Firestore.
"""

import json
import os
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, url_for

app = Flask(__name__)

# Configuration
USE_LOCAL_DB = os.environ.get("USE_LOCAL_DB", "1") == "1"
LOCAL_DB_PATH = Path(__file__).parent / "local_content.json"
COLLECTION = "poker_content"

# Database abstraction
if USE_LOCAL_DB:
    print(f"Using local JSON database: {LOCAL_DB_PATH}")

    def _load_local_db():
        if LOCAL_DB_PATH.exists():
            with open(LOCAL_DB_PATH) as f:
                return json.load(f)
        return {}

    def _save_local_db(data):
        with open(LOCAL_DB_PATH, "w") as f:
            json.dump(data, f, indent=2)

    class LocalDB:
        def collection(self, name):
            return LocalCollection(name)

    class LocalCollection:
        def __init__(self, name):
            self.name = name

        def stream(self):
            data = _load_local_db()
            for doc_id, doc_data in data.items():
                yield LocalDoc(doc_id, doc_data)

        def document(self, doc_id):
            return LocalDocRef(doc_id)

    class LocalDoc:
        def __init__(self, id, data):
            self.id = id
            self._data = data
            self.exists = True

        def to_dict(self):
            return self._data

        @property
        def reference(self):
            return LocalDocRef(self.id)

    class LocalDocRef:
        def __init__(self, doc_id):
            self.id = doc_id

        def get(self):
            data = _load_local_db()
            if self.id in data:
                return LocalDoc(self.id, data[self.id])
            doc = LocalDoc(self.id, {})
            doc.exists = False
            return doc

        def set(self, doc_data):
            data = _load_local_db()
            data[self.id] = doc_data
            _save_local_db(data)

        def update(self, updates):
            data = _load_local_db()
            if self.id in data:
                data[self.id].update(updates)
                _save_local_db(data)

        def delete(self):
            data = _load_local_db()
            if self.id in data:
                del data[self.id]
                _save_local_db(data)

    db = LocalDB()
else:
    from google.cloud import firestore
    db = firestore.Client()
    print("Using Firestore database")


def get_all_content():
    """Fetch all poker content."""
    docs = db.collection(COLLECTION).stream()
    content = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        content.append(data)
    return sorted(content, key=lambda x: (x.get("category", ""), x.get("name", "")))


@app.route("/")
def index():
    """Main content list page."""
    content = get_all_content()

    # Group by category
    by_category = {}
    for item in content:
        cat = item.get("category", "uncategorized")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)

    # Stats
    stats = {
        "total": len(content),
        "terms": len([c for c in content if c.get("type") == "term"]),
        "concepts": len([c for c in content if c.get("type") == "concept"]),
        "with_blurb": len([c for c in content if c.get("blurb")]),
    }

    return render_template("index.html",
                          by_category=by_category,
                          stats=stats,
                          content=content)


@app.route("/content/<content_id>")
def view_content(content_id):
    """View/edit a single content item."""
    doc = db.collection(COLLECTION).document(content_id).get()
    if not doc.exists:
        return "Not found", 404

    data = doc.to_dict()
    data["id"] = doc.id
    return render_template("edit.html", item=data)


@app.route("/content/<content_id>/update", methods=["POST"])
def update_content(content_id):
    """Update a content item."""
    doc_ref = db.collection(COLLECTION).document(content_id)

    updates = {}
    for field in ["name", "blurb", "body", "category", "type"]:
        if field in request.form:
            value = request.form[field].strip()
            updates[field] = value if value else None

    doc_ref.update(updates)
    return redirect(url_for("view_content", content_id=content_id))


@app.route("/content/new", methods=["GET", "POST"])
def new_content():
    """Create new content item."""
    if request.method == "POST":
        content_id = request.form["id"].strip().lower().replace(" ", "-")

        data = {
            "id": content_id,
            "name": request.form["name"].strip(),
            "type": request.form["type"],
            "category": request.form["category"].strip(),
            "blurb": request.form.get("blurb", "").strip() or None,
            "body": request.form.get("body", "").strip() or None,
        }

        db.collection(COLLECTION).document(content_id).set(data)
        return redirect(url_for("view_content", content_id=content_id))

    return render_template("new.html")


@app.route("/content/<content_id>/delete", methods=["POST"])
def delete_content(content_id):
    """Delete a content item."""
    db.collection(COLLECTION).document(content_id).delete()
    return redirect(url_for("index"))


@app.route("/api/content")
def api_list_content():
    """API endpoint to list all content."""
    return jsonify(get_all_content())


@app.route("/api/content/<content_id>")
def api_get_content(content_id):
    """API endpoint to get single content item."""
    doc = db.collection(COLLECTION).document(content_id).get()
    if not doc.exists:
        return jsonify({"error": "not found"}), 404
    data = doc.to_dict()
    data["id"] = doc.id
    return jsonify(data)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
