from __future__ import annotations

import functools
import hashlib
import html
import re
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    Response,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "library.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"
app.config["DATABASE"] = DB_PATH


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            middle_name TEXT,
            role_id INTEGER NOT NULL,
            FOREIGN KEY (role_id) REFERENCES roles(id)
        );

        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            year INTEGER NOT NULL,
            publisher TEXT NOT NULL,
            author TEXT NOT NULL,
            pages INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS book_genres (
            book_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            PRIMARY KEY (book_id, genre_id),
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
            FOREIGN KEY (genre_id) REFERENCES genres(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS covers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            md5_hash TEXT NOT NULL,
            book_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 0 AND 5),
            text TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (book_id, user_id),
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS collection_books (
            collection_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            PRIMARY KEY (collection_id, book_id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
        );
        """
    )

    role_count = db.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    if role_count:
        return

    roles = [
        ("Администратор", "Полный доступ к системе"),
        ("Модератор", "Редактирование книг и модерация рецензий"),
        ("Пользователь", "Просмотр книг, рецензии и подборки"),
    ]
    db.executemany("INSERT INTO roles (name, description) VALUES (?, ?)", roles)

    role_ids = {
        row["name"]: row["id"]
        for row in db.execute("SELECT id, name FROM roles").fetchall()
    }
    users = [
        ("admin", "admin", "Невретдинов", "Илья", "Администраторович", "Администратор"),
        ("moderator", "moderator", "Невретдинов", "Илья", "Модераторович", "Модератор"),
        ("user", "user", "Невретдинов", "Илья", "Пользователевич", "Пользователь"),
    ]
    db.executemany(
        """
        INSERT INTO users
            (login, password_hash, last_name, first_name, middle_name, role_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                login,
                generate_password_hash(password),
                last_name,
                first_name,
                middle_name,
                role_ids[role],
            )
            for login, password, last_name, first_name, middle_name, role in users
        ],
    )

    demo_books = [
        (
            "Мастер и Маргарита",
            "Роман о Москве, мистике и выборе человека.\n\n**Классика**, которую удобно обсуждать.",
            1967,
            "YMCA-Press",
            "Михаил Булгаков",
            480,
            ["Роман", "Классика"],
        ),
        (
            "Преступление и наказание",
            "История Раскольникова и его нравственного испытания.",
            1866,
            "Русский вестник",
            "Федор Достоевский",
            672,
            ["Роман", "Классика"],
        ),
        (
            "Пикник на обочине",
            "Фантастическая повесть о Зоне и людях, которые пытаются понять ее правила.",
            1972,
            "Аврора",
            "Аркадий и Борис Стругацкие",
            192,
            ["Фантастика"],
        ),
    ]
    for title, description, year, publisher, author, pages, genres in demo_books:
        book_id = create_book(db, title, description, year, publisher, author, pages, genres)
        add_cover_record(db, book_id, title)

    user_id = db.execute("SELECT id FROM users WHERE login = 'user'").fetchone()["id"]
    db.execute("INSERT INTO collections (title, user_id) VALUES (?, ?)", ("Любимые книги", user_id))
    collection_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO collection_books (collection_id, book_id) VALUES (?, ?)",
        (collection_id, 1),
    )
    db.commit()


@app.cli.command("init-db")
def init_db_command() -> None:
    init_db()
    print("Database initialized")


@app.before_request
def load_user() -> None:
    init_db()
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return
    g.user = get_db().execute(
        """
        SELECT users.*, roles.name AS role_name
        FROM users JOIN roles ON roles.id = users.role_id
        WHERE users.id = ?
        """,
        (user_id,),
    ).fetchone()


@app.context_processor
def inject_globals() -> dict:
    return {
        "current_user": g.get("user"),
        "is_user": has_role("Пользователь"),
        "is_moderator": has_role("Модератор"),
        "is_admin": has_role("Администратор"),
    }


def has_role(role_name: str) -> bool:
    user = g.get("user")
    return bool(user and user["role_name"] == role_name)


def full_name(user: sqlite3.Row) -> str:
    parts = [user["last_name"], user["first_name"], user["middle_name"] or ""]
    return " ".join(part for part in parts if part)


app.jinja_env.globals["full_name"] = full_name


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("Для выполнения данного действия необходимо пройти процедуру аутентификации", "warning")
            return redirect(url_for("login", next=request.full_path))
        return view(**kwargs)

    return wrapped_view


def roles_required(*role_names: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapped_view(**kwargs):
            if g.user is None:
                flash("Для выполнения данного действия необходимо пройти процедуру аутентификации", "warning")
                return redirect(url_for("login", next=request.full_path))
            if g.user["role_name"] not in role_names:
                flash("У вас недостаточно прав для выполнения данного действия", "danger")
                return redirect(url_for("index"))
            return view(**kwargs)

        return wrapped_view

    return decorator


def sanitize_text(value: str) -> str:
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", "", value)
    cleaned = re.sub(r"(?is)</?\w+[^>]*>", "", cleaned)
    return cleaned.strip()


def render_markdown(value: str) -> str:
    text = html.escape(value or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    paragraphs = [p.strip().replace("\n", "<br>") for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)


app.jinja_env.filters["markdown"] = render_markdown


def create_book(
    db: sqlite3.Connection,
    title: str,
    description: str,
    year: int,
    publisher: str,
    author: str,
    pages: int,
    genre_names: list[str],
) -> int:
    cursor = db.execute(
        """
        INSERT INTO books (title, description, year, publisher, author, pages)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (title, description, year, publisher, author, pages),
    )
    book_id = cursor.lastrowid
    sync_book_genres(db, book_id, genre_names)
    return book_id


def sync_book_genres(db: sqlite3.Connection, book_id: int, genre_names: list[str]) -> None:
    db.execute("DELETE FROM book_genres WHERE book_id = ?", (book_id,))
    for raw_name in genre_names:
        name = raw_name.strip()
        if not name:
            continue
        db.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
        genre_id = db.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()["id"]
        db.execute(
            "INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
            (book_id, genre_id),
        )


def add_cover_record(db: sqlite3.Connection, book_id: int, title: str) -> None:
    md5_hash = hashlib.md5(title.encode("utf-8")).hexdigest()
    db.execute(
        """
        INSERT OR IGNORE INTO covers (filename, mime_type, md5_hash, book_id)
        VALUES (?, ?, ?, ?)
        """,
        (f"book-{book_id}.svg", "image/svg+xml", md5_hash, book_id),
    )


def get_book_or_404(book_id: int) -> sqlite3.Row:
    book = get_db().execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        abort(404)
    return book


@app.route("/")
def index():
    init_db()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 10
    offset = (page - 1) * per_page
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    books = db.execute(
        """
        SELECT
            books.*,
            COALESCE(GROUP_CONCAT(DISTINCT genres.name), '') AS genres,
            ROUND(AVG(reviews.rating), 1) AS avg_rating,
            COUNT(DISTINCT reviews.id) AS review_count
        FROM books
        LEFT JOIN book_genres ON book_genres.book_id = books.id
        LEFT JOIN genres ON genres.id = book_genres.genre_id
        LEFT JOIN reviews ON reviews.book_id = books.id
        GROUP BY books.id
        ORDER BY books.created_at DESC, books.id DESC
        LIMIT ? OFFSET ?
        """,
        (per_page, offset),
    ).fetchall()
    pages = max((total + per_page - 1) // per_page, 1)
    return render_template("index.html", books=books, page=page, pages=pages)


@app.route("/login", methods=("GET", "POST"))
def login():
    init_db()
    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            """
            SELECT users.*, roles.name AS role_name
            FROM users JOIN roles ON roles.id = users.role_id
            WHERE login = ?
            """,
            (login_value,),
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = bool(request.form.get("remember"))
            session["user_id"] = user["id"]
            flash("Вы успешно вошли в систему", "success")
            return redirect(request.args.get("next") or url_for("index"))
        flash("Невозможно аутентифицироваться с указанными логином и паролем", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из системы", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/books/add", methods=("GET", "POST"))
@roles_required("Администратор")
def add_book():
    if request.method == "POST":
        try:
            title, description, year, publisher, author, pages, genres = parse_book_form()
            db = get_db()
            book_id = create_book(db, title, description, year, publisher, author, pages, genres)
            add_cover_record(db, book_id, title)
            db.commit()
            flash("Книга успешно добавлена", "success")
            return redirect(url_for("book_detail", book_id=book_id))
        except (ValueError, sqlite3.Error):
            get_db().rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")
    return render_template("book_form.html", book=None, genres="")


@app.route("/books/<int:book_id>/edit", methods=("GET", "POST"))
@roles_required("Администратор", "Модератор")
def edit_book(book_id: int):
    book = get_book_or_404(book_id)
    db = get_db()
    if request.method == "POST":
        try:
            title, description, year, publisher, author, pages, genres = parse_book_form()
            db.execute(
                """
                UPDATE books
                SET title = ?, description = ?, year = ?, publisher = ?, author = ?, pages = ?
                WHERE id = ?
                """,
                (title, description, year, publisher, author, pages, book_id),
            )
            sync_book_genres(db, book_id, genres)
            db.commit()
            flash("Книга успешно обновлена", "success")
            return redirect(url_for("book_detail", book_id=book_id))
        except (ValueError, sqlite3.Error):
            db.rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")

    genres = ", ".join(
        row["name"]
        for row in db.execute(
            """
            SELECT genres.name
            FROM genres JOIN book_genres ON book_genres.genre_id = genres.id
            WHERE book_genres.book_id = ?
            ORDER BY genres.name
            """,
            (book_id,),
        ).fetchall()
    )
    return render_template("book_form.html", book=book, genres=genres)


def parse_book_form() -> tuple[str, str, int, str, str, int, list[str]]:
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    publisher = request.form.get("publisher", "").strip()
    author = request.form.get("author", "").strip()
    genres = [name.strip() for name in request.form.get("genres", "").split(",")]
    year = request.form.get("year", type=int)
    pages = request.form.get("pages", type=int)
    if not title or not description or not publisher or not author or not year or not pages:
        raise ValueError("Invalid book form")
    return title, sanitize_text(description), year, publisher, author, pages, genres


@app.route("/books/<int:book_id>/delete", methods=("POST",))
@roles_required("Администратор")
def delete_book(book_id: int):
    get_book_or_404(book_id)
    db = get_db()
    db.execute("DELETE FROM books WHERE id = ?", (book_id,))
    db.commit()
    flash("Книга успешно удалена", "success")
    return redirect(url_for("index"))


@app.route("/books/<int:book_id>")
def book_detail(book_id: int):
    init_db()
    db = get_db()
    book = get_book_or_404(book_id)
    genres = db.execute(
        """
        SELECT genres.name
        FROM genres JOIN book_genres ON book_genres.genre_id = genres.id
        WHERE book_genres.book_id = ?
        ORDER BY genres.name
        """,
        (book_id,),
    ).fetchall()
    reviews = db.execute(
        """
        SELECT reviews.*, users.last_name, users.first_name, users.middle_name
        FROM reviews JOIN users ON users.id = reviews.user_id
        WHERE reviews.book_id = ?
        ORDER BY reviews.created_at DESC
        """,
        (book_id,),
    ).fetchall()
    user_review = None
    collections = []
    if g.user:
        user_review = db.execute(
            "SELECT * FROM reviews WHERE book_id = ? AND user_id = ?",
            (book_id, g.user["id"]),
        ).fetchone()
    if has_role("Пользователь"):
        collections = db.execute(
            "SELECT * FROM collections WHERE user_id = ? ORDER BY title",
            (g.user["id"],),
        ).fetchall()
    return render_template(
        "book_detail.html",
        book=book,
        genres=genres,
        reviews=reviews,
        user_review=user_review,
        collections=collections,
    )


@app.route("/covers/<int:book_id>.svg")
def cover(book_id: int):
    book = get_book_or_404(book_id)
    title = html.escape(book["title"])
    author = html.escape(book["author"])
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="420" height="620" viewBox="0 0 420 620">
      <rect width="420" height="620" fill="#f4efe4"/>
      <rect x="28" y="28" width="364" height="564" rx="10" fill="#315c65"/>
      <rect x="50" y="50" width="320" height="520" rx="6" fill="#f9f6ee"/>
      <text x="210" y="220" text-anchor="middle" font-family="Arial" font-size="34" font-weight="700" fill="#263238">
        <tspan x="210">{title[:22]}</tspan>
      </text>
      <text x="210" y="300" text-anchor="middle" font-family="Arial" font-size="22" fill="#566">
        <tspan x="210">{author[:28]}</tspan>
      </text>
      <text x="210" y="500" text-anchor="middle" font-family="Arial" font-size="18" fill="#315c65">Электронная библиотека</text>
    </svg>
    """
    return Response(svg, mimetype="image/svg+xml")


@app.route("/books/<int:book_id>/reviews/add", methods=("GET", "POST"))
@roles_required("Пользователь", "Модератор", "Администратор")
def add_review(book_id: int):
    book = get_book_or_404(book_id)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM reviews WHERE book_id = ? AND user_id = ?",
        (book_id, g.user["id"]),
    ).fetchone()
    if existing:
        flash("Вы уже оставили рецензию на эту книгу", "warning")
        return redirect(url_for("book_detail", book_id=book_id))
    if request.method == "POST":
        rating = request.form.get("rating", type=int)
        text = request.form.get("text", "").strip()
        if rating is None or rating < 0 or rating > 5 or not text:
            flash("Проверьте корректность введённых данных", "danger")
        else:
            db.execute(
                "INSERT INTO reviews (book_id, user_id, rating, text) VALUES (?, ?, ?, ?)",
                (book_id, g.user["id"], rating, sanitize_text(text)),
            )
            db.commit()
            flash("Рецензия успешно добавлена", "success")
            return redirect(url_for("book_detail", book_id=book_id))
    return render_template("review_form.html", book=book)


@app.route("/collections")
@roles_required("Пользователь")
def collections():
    rows = get_db().execute(
        """
        SELECT collections.*, COUNT(collection_books.book_id) AS book_count
        FROM collections
        LEFT JOIN collection_books ON collection_books.collection_id = collections.id
        WHERE collections.user_id = ?
        GROUP BY collections.id
        ORDER BY collections.created_at DESC, collections.id DESC
        """,
        (g.user["id"],),
    ).fetchall()
    return render_template("collections.html", collections=rows)


@app.route("/collections/add", methods=("POST",))
@roles_required("Пользователь")
def add_collection():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Введите название подборки", "danger")
        return redirect(url_for("collections"))
    db = get_db()
    db.execute(
        "INSERT INTO collections (title, user_id) VALUES (?, ?)",
        (title, g.user["id"]),
    )
    db.commit()
    flash("Подборка успешно добавлена", "success")
    return redirect(url_for("collections"))


@app.route("/collections/<int:collection_id>")
@roles_required("Пользователь")
def collection_detail(collection_id: int):
    db = get_db()
    collection = db.execute(
        "SELECT * FROM collections WHERE id = ? AND user_id = ?",
        (collection_id, g.user["id"]),
    ).fetchone()
    if collection is None:
        abort(404)
    books = db.execute(
        """
        SELECT books.*, COALESCE(GROUP_CONCAT(DISTINCT genres.name), '') AS genres
        FROM books
        JOIN collection_books ON collection_books.book_id = books.id
        LEFT JOIN book_genres ON book_genres.book_id = books.id
        LEFT JOIN genres ON genres.id = book_genres.genre_id
        WHERE collection_books.collection_id = ?
        GROUP BY books.id
        ORDER BY books.title
        """,
        (collection_id,),
    ).fetchall()
    return render_template("collection_detail.html", collection=collection, books=books)


@app.route("/books/<int:book_id>/collections/add", methods=("POST",))
@roles_required("Пользователь")
def add_book_to_collection(book_id: int):
    get_book_or_404(book_id)
    collection_id = request.form.get("collection_id", type=int)
    db = get_db()
    collection = db.execute(
        "SELECT id FROM collections WHERE id = ? AND user_id = ?",
        (collection_id, g.user["id"]),
    ).fetchone()
    if collection is None:
        flash("Выберите существующую подборку", "danger")
        return redirect(url_for("book_detail", book_id=book_id))
    db.execute(
        "INSERT OR IGNORE INTO collection_books (collection_id, book_id) VALUES (?, ?)",
        (collection_id, book_id),
    )
    db.commit()
    flash("Книга успешно добавлена в подборку", "success")
    return redirect(url_for("book_detail", book_id=book_id))


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True)
