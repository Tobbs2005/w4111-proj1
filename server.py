"""
COMS W4111 Project 1, Part 3: Conference Peer Review System
Team: Cecilia Yang (zy2644), Ziyue (Toby) Fang (zf2389)

The app has the four views from our Part 1 proposal, plus a page for
managing people, conferences and topics:
  - Public view:    search accepted papers; accepted papers by an author
  - Author view:    submit a paper, see own papers and their status
  - Reviewer view:  search papers, bid, see assignments, submit reviews
  - Organizer view: per-conference papers, bids, assignments, reviews,
                    reviewer suggestions, decisions, topic statistics
There is no login: as in the proposal, the user picks an author or a
reviewer from a menu on the home page.

All database access is hand-written SQL run through SQLAlchemy's text().
User input is always passed as a bound parameter (:name), never pasted
into the SQL string, so the app is not open to SQL injection.
"""
import os
import re
from datetime import date, timedelta
from functools import lru_cache

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "w4111-proj1-part3")

@lru_cache(maxsize=1)
def get_engine():
    """Create the connection pool on first use (same settings as the
    course starter: one connection per Cloud Run instance, short timeouts)."""
    database_url = URL.create(
        "postgresql+psycopg2",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5454")),
        database=os.environ.get("DB_NAME", "proj1part2"),
    )
    return create_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )


RECOMMENDATIONS = ["strong accept", "accept", "weak accept", "weak reject", "reject", "strong reject"]


# =====================================================================
# Helpers
# =====================================================================

@app.before_request
def before_request():
    g.conn = None
    if request.endpoint in (None, "static"):
        return
    if not (os.environ.get("DB_USER") and os.environ.get("DB_PASSWORD")):
        return render_template("error.html", message="Database credentials are not configured."), 503
    try:
        g.conn = get_engine().connect()
        schema = os.environ.get("DB_SCHEMA")
        if schema:
            # Bound parameter, quoted as one identifier (as in the starter).
            g.conn.execute(text("SELECT set_config('search_path', quote_ident(:schema), false)"),
                           {"schema": schema})
            g.conn.commit()
    except (SQLAlchemyError, ValueError, KeyError) as exc:
        # Driver messages can contain connection details, so log only the type.
        app.logger.warning("Database connection failed (%s)", type(exc).__name__)
        if g.conn is not None:
            g.conn.close()
            g.conn = None
        return render_template("error.html", message="The database is unavailable right now."), 503


@app.teardown_request
def teardown_request(exception):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


@app.route("/db-check")
def database_check():
    """Health check used by the course's Cloud Run setup guide."""
    value = g.conn.execute(text("SELECT :value"), {"value": 1}).scalar_one()
    schema_ok = g.conn.execute(text("""
        SELECT current_schema() IS NOT NULL AND has_schema_privilege(current_schema(), 'USAGE')
    """)).scalar_one()
    papers = g.conn.execute(text("SELECT to_regclass('papers') IS NOT NULL")).scalar_one()
    return jsonify(database="ok", select_1=value, current_schema_accessible=bool(schema_ok),
                   project_tables_exist=bool(papers), revision=os.environ.get("K_REVISION", "local"))


def query(sql, **params):
    """Run a SELECT and return all rows (columns accessible by name)."""
    return g.conn.execute(text(sql), params).mappings().all()


def query_one(sql, **params):
    return g.conn.execute(text(sql), params).mappings().first()


def execute(sql, **params):
    """Run an INSERT/UPDATE/DELETE inside the current transaction."""
    return g.conn.execute(text(sql), params)


def lock(table):
    """Our primary keys are plain INTEGERs (no SERIAL), so new ids are
    computed as MAX(id) + 1. Locking the table for the rest of the
    transaction stops two requests from picking the same id."""
    # table is always one of our own constant table names, never user input
    execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")


def db_error_message(e):
    orig = getattr(e, "orig", None)
    return (str(orig) if orig is not None else str(e)).split("\n")[0]


def commit_or_report(success_msg):
    """Commit the current transaction, or roll back and show the
    constraint that the database rejected (CHECK, UNIQUE, FK, ...)."""
    try:
        g.conn.commit()
        flash(success_msg, "ok")
        return True
    except DBAPIError as e:
        g.conn.rollback()
        flash("The database rejected this change: " + db_error_message(e), "error")
        return False


class FormError(Exception):
    pass


def form_str(name, required=True):
    v = request.form.get(name, "").strip()
    if required and not v:
        raise FormError(f"Please fill in '{name.replace('_', ' ')}'.")
    return v or None


def form_int(name, required=True):
    v = request.form.get(name, "").strip()
    if not v:
        if required:
            raise FormError(f"Please fill in '{name.replace('_', ' ')}'.")
        return None
    try:
        return int(v)
    except ValueError:
        raise FormError(f"'{name.replace('_', ' ')}' must be a whole number.")


def form_date(name, default=None):
    v = request.form.get(name, "").strip()
    if not v:
        return default
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise FormError(f"'{name.replace('_', ' ')}' must be a date.")


def run_write(action, redirect_to):
    """Run a write handler; turn FormError and DB errors into messages."""
    try:
        action()
    except FormError as e:
        g.conn.rollback()
        flash(str(e), "error")
    except DBAPIError as e:
        g.conn.rollback()
        flash("The database rejected this change: " + db_error_message(e), "error")
    return redirect(redirect_to)


def get_person(person_id, role):
    """Return the person if they have the given role ('Authors' or 'Reviewers')."""
    if role not in ("Authors", "Reviewers"):
        abort(500)
    row = query_one(f"""
        SELECT pe.person_id, pe.name, pe.email, pe.affiliation
        FROM People pe JOIN {role} x ON x.person_id = pe.person_id
        WHERE pe.person_id = :pid
    """, pid=person_id)
    if row is None:
        abort(404)
    return row


def all_conferences():
    return query("SELECT conference_id, name, year, deadline FROM Conferences ORDER BY year DESC, name")


def all_topics():
    return query("SELECT topic_id, topic_name FROM Topics ORDER BY topic_name")


# =====================================================================
# Home: choose a view (and, for authors/reviewers, who you are)
# =====================================================================

@app.route("/")
def index():
    authors = query("""
        SELECT pe.person_id, pe.name FROM Authors a JOIN People pe ON pe.person_id = a.person_id
        ORDER BY pe.name
    """)
    reviewers = query("""
        SELECT pe.person_id, pe.name, pe.affiliation FROM Reviewers r JOIN People pe ON pe.person_id = r.person_id
        ORDER BY pe.name
    """)
    counts = query_one("""
        SELECT (SELECT COUNT(*) FROM Conferences) AS conferences, (SELECT COUNT(*) FROM Papers) AS papers,
               (SELECT COUNT(*) FROM People) AS people, (SELECT COUNT(*) FROM Reviews) AS reviews,
               (SELECT COUNT(*) FROM Decisions WHERE outcome = 'accept') AS accepted
    """)
    return render_template("index.html", authors=authors, reviewers=reviewers, counts=counts)


@app.route("/go")
def go():
    """Handles the 'pick who you are' boxes on the home page. Each box is a
    text input backed by a <datalist>, so the submitted value is either a
    suggestion of the form "Name (#id)" or a name the user typed freely."""
    role = request.args.get("role")
    if role == "author":
        table, endpoint, arg = "Authors", "author_home", "aid"
    elif role == "reviewer":
        table, endpoint, arg = "Reviewers", "reviewer_home", "rid"
    else:
        return redirect(url_for("index"))

    pid = request.args.get("person_id", type=int)
    typed = request.args.get("person", "").strip()
    if pid is None and typed:
        chosen = re.search(r"\(#(\d+)\)\s*$", typed)
        if chosen:
            pid = int(chosen.group(1))
        else:
            # A free-typed name: use it if it is the unique prefix of one
            # person with this role. table is one of our two constant
            # table names above, never user input.
            matches = query(f"""
                SELECT pe.person_id
                FROM People pe JOIN {table} x ON x.person_id = pe.person_id
                WHERE pe.name ILIKE :pat ORDER BY pe.person_id LIMIT 2
            """, pat=typed + "%")
            if len(matches) == 1:
                pid = matches[0]["person_id"]
    if pid is None:
        if typed:
            flash(f"No single {role} matches '{typed}'. Pick a name from the suggestions.", "error")
        return redirect(url_for("index"))
    return redirect(url_for(endpoint, **{arg: pid}))


# =====================================================================
# Public view
# =====================================================================

@app.route("/public")
def public_search():
    """Search accepted papers by title, author name, topic and conference."""
    title = request.args.get("title", "").strip()
    author = request.args.get("author", "").strip()
    topic_id = request.args.get("topic_id", type=int)
    conf_id = request.args.get("conference_id", type=int)

    papers = query("""
        SELECT p.paper_id, p.title, c.name AS conf_name, c.year, d.comments AS decision_note,
               (SELECT STRING_AGG(pe.name, ', ' ORDER BY pe.name)
                  FROM Writes w JOIN People pe ON pe.person_id = w.author_id
                 WHERE w.paper_id = p.paper_id) AS authors,
               (SELECT STRING_AGG(t.topic_name, '; ' ORDER BY t.topic_name)
                  FROM Has_Topic ht JOIN Topics t ON t.topic_id = ht.topic_id
                 WHERE ht.paper_id = p.paper_id) AS topics
        FROM Papers p
        JOIN Conferences c ON c.conference_id = p.conference_id
        JOIN Decisions d   ON d.paper_id = p.paper_id AND d.outcome = 'accept'
        WHERE p.title ILIKE :title_pat
          AND (:author = '' OR EXISTS (
                SELECT 1 FROM Writes w JOIN People pe ON pe.person_id = w.author_id
                WHERE w.paper_id = p.paper_id AND pe.name ILIKE :author_pat))
          AND (CAST(:topic_id AS INTEGER) IS NULL OR EXISTS (
                SELECT 1 FROM Has_Topic ht WHERE ht.paper_id = p.paper_id AND ht.topic_id = :topic_id))
          AND (CAST(:conf_id AS INTEGER) IS NULL OR p.conference_id = :conf_id)
        ORDER BY c.year DESC, p.title
    """, title_pat=f"%{title}%", author=author, author_pat=f"%{author}%", topic_id=topic_id, conf_id=conf_id)

    return render_template("public_search.html", papers=papers, title=title, author=author,
                           topic_id=topic_id, conf_id=conf_id,
                           topics=all_topics(), conferences=all_conferences())


@app.route("/public/author/<int:aid>")
def public_author(aid):
    person = get_person(aid, "Authors")
    papers = query("""
        SELECT p.paper_id, p.title, c.name AS conf_name, c.year, d.comments AS decision_note
        FROM Writes w
        JOIN Papers p      ON p.paper_id = w.paper_id
        JOIN Conferences c ON c.conference_id = p.conference_id
        JOIN Decisions d   ON d.paper_id = p.paper_id AND d.outcome = 'accept'
        WHERE w.author_id = :aid
        ORDER BY c.year DESC, p.title
    """, aid=aid)
    return render_template("public_author.html", person=person, papers=papers)


@app.route("/paper/<int:pid>")
def paper(pid):
    """Basic paper information (no reviews, which only organizers see)."""
    p = query_one("""
        SELECT p.*, c.name AS conf_name, c.year, d.outcome, d.comments AS decision_note
        FROM Papers p
        JOIN Conferences c   ON c.conference_id = p.conference_id
        LEFT JOIN Decisions d ON d.paper_id = p.paper_id
        WHERE p.paper_id = :pid
    """, pid=pid)
    if p is None:
        abort(404)
    authors = query("""
        SELECT pe.person_id, pe.name, pe.affiliation
        FROM Writes w JOIN People pe ON pe.person_id = w.author_id
        WHERE w.paper_id = :pid ORDER BY pe.name
    """, pid=pid)
    topics = query("""
        SELECT t.topic_id, t.topic_name FROM Has_Topic ht JOIN Topics t ON t.topic_id = ht.topic_id
        WHERE ht.paper_id = :pid ORDER BY t.topic_name
    """, pid=pid)
    return render_template("paper.html", p=p, authors=authors, topics=topics)


# =====================================================================
# Author view
# =====================================================================

@app.route("/author/<int:aid>")
def author_home(aid):
    person = get_person(aid, "Authors")
    papers = query("""
        SELECT p.paper_id, p.title, p.submitted_on, c.name AS conf_name, c.year,
               COALESCE(d.outcome, 'pending') AS status, d.comments AS decision_note
        FROM Writes w
        JOIN Papers p        ON p.paper_id = w.paper_id
        JOIN Conferences c   ON c.conference_id = p.conference_id
        LEFT JOIN Decisions d ON d.paper_id = p.paper_id
        WHERE w.author_id = :aid
        ORDER BY p.submitted_on DESC, p.title
    """, aid=aid)
    coauthors = query("""
        SELECT pe.person_id, pe.name FROM Authors a JOIN People pe ON pe.person_id = a.person_id
        WHERE a.person_id <> :aid ORDER BY pe.name
    """, aid=aid)
    return render_template("author_home.html", person=person, papers=papers, coauthors=coauthors,
                           conferences=all_conferences(), topics=all_topics())


@app.route("/author/<int:aid>/submit", methods=["POST"])
def author_submit(aid):
    """Submit a paper. One transaction inserts into Papers (Submitted_To),
    Writes and Has_Topic, so the app never stores a paper without at least
    one author and one topic, the two total participation constraints our
    schema cannot enforce on its own."""
    get_person(aid, "Authors")

    def action():
        title = form_str("title")
        abstract = form_str("abstract")
        conference_id = form_int("conference_id")
        topic_ids = sorted({int(x) for x in request.form.getlist("topic_ids") if x.isdigit()})
        author_ids = sorted({aid} | {int(x) for x in request.form.getlist("coauthor_ids") if x.isdigit()})
        if not topic_ids:
            raise FormError("Pick at least one topic.")

        lock("Papers")
        paper_id = execute("""
            INSERT INTO Papers (paper_id, title, abstract, submitted_on, conference_id)
            SELECT COALESCE(MAX(paper_id), 0) + 1, :title, :abstract, CURRENT_DATE, :conference_id
            FROM Papers
            RETURNING paper_id
        """, title=title, abstract=abstract, conference_id=conference_id).scalar_one()
        for a in author_ids:
            execute("INSERT INTO Writes (author_id, paper_id) VALUES (:a, :p)", a=a, p=paper_id)
        for t in topic_ids:
            execute("INSERT INTO Has_Topic (paper_id, topic_id) VALUES (:p, :t)", p=paper_id, t=t)
        commit_or_report(f"Paper #{paper_id} submitted.")

    return run_write(action, url_for("author_home", aid=aid))


# =====================================================================
# Reviewer view
# =====================================================================

@app.route("/reviewer/<int:rid>")
def reviewer_home(rid):
    person = get_person(rid, "Reviewers")
    q = request.args.get("q", "").strip()
    conf_id = request.args.get("conference_id", type=int)
    topic_id = request.args.get("topic_id", type=int)

    assignments = query("""
        SELECT p.paper_id, p.title, a.assigned_on, a.due_on,
               rv.score, rv.confidence, rv.recommendation,
               (rv.paper_id IS NULL AND a.due_on < CURRENT_DATE) AS overdue
        FROM Assigned_To a
        JOIN Papers p ON p.paper_id = a.paper_id
        LEFT JOIN Reviews rv ON rv.reviewer_id = a.reviewer_id AND rv.paper_id = a.paper_id
        WHERE a.reviewer_id = :rid
        ORDER BY (rv.paper_id IS NULL) DESC, a.due_on, p.title
    """, rid=rid)

    # Papers to browse and bid on. "conflict" marks papers this reviewer
    # wrote (Authors and Reviewers overlap), which they may not bid on.
    papers = query("""
        SELECT p.paper_id, p.title, c.name AS conf_name, c.year, b.bid_value,
               EXISTS (SELECT 1 FROM Writes w WHERE w.paper_id = p.paper_id AND w.author_id = :rid) AS conflict,
               EXISTS (SELECT 1 FROM Decisions d WHERE d.paper_id = p.paper_id) AS decided
        FROM Papers p
        JOIN Conferences c ON c.conference_id = p.conference_id
        LEFT JOIN Bids_On b ON b.paper_id = p.paper_id AND b.reviewer_id = :rid
        WHERE (p.title ILIKE :pat OR p.abstract ILIKE :pat)
          AND (CAST(:conf_id AS INTEGER) IS NULL OR p.conference_id = :conf_id)
          AND (CAST(:topic_id AS INTEGER) IS NULL OR EXISTS (
                SELECT 1 FROM Has_Topic ht WHERE ht.paper_id = p.paper_id AND ht.topic_id = :topic_id))
        ORDER BY c.year DESC, p.title
    """, rid=rid, pat=f"%{q}%", conf_id=conf_id, topic_id=topic_id)

    return render_template("reviewer_home.html", person=person, assignments=assignments, papers=papers,
                           q=q, conf_id=conf_id, topic_id=topic_id,
                           conferences=all_conferences(), topics=all_topics())


@app.route("/reviewer/<int:rid>/bid", methods=["POST"])
def reviewer_bid(rid):
    get_person(rid, "Reviewers")

    def action():
        paper_id = form_int("paper_id")
        bid_value = form_int("bid_value")
        # Place or update the bid in one statement. The WHERE NOT EXISTS
        # blocks bidding on your own paper (no triggers, so the app checks).
        result = execute("""
            INSERT INTO Bids_On (reviewer_id, paper_id, bid_value)
            SELECT :rid, :pid, :bid
            WHERE NOT EXISTS (SELECT 1 FROM Writes w WHERE w.paper_id = :pid AND w.author_id = :rid)
            ON CONFLICT (reviewer_id, paper_id) DO UPDATE SET bid_value = EXCLUDED.bid_value
        """, rid=rid, pid=paper_id, bid=bid_value)
        if result.rowcount == 0:
            g.conn.rollback()
            raise FormError("You cannot bid on a paper you wrote.")
        commit_or_report("Bid saved.")

    # Only follow "next" if it is a path on this site (no open redirect).
    nxt = request.form.get("next", "")
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = url_for("reviewer_home", rid=rid)
    return run_write(action, nxt)


@app.route("/reviewer/<int:rid>/review/<int:pid>", methods=["GET", "POST"])
def reviewer_review(rid, pid):
    person = get_person(rid, "Reviewers")
    assignment = query_one("""
        SELECT a.assigned_on, a.due_on, p.paper_id, p.title, p.abstract
        FROM Assigned_To a JOIN Papers p ON p.paper_id = a.paper_id
        WHERE a.reviewer_id = :rid AND a.paper_id = :pid
    """, rid=rid, pid=pid)
    if assignment is None:
        flash("You can only review papers assigned to you.", "error")
        return redirect(url_for("reviewer_home", rid=rid))

    if request.method == "POST":
        def action():
            rec = form_str("recommendation")
            if rec not in RECOMMENDATIONS:
                raise FormError("Pick a recommendation from the list.")
            # Reviews has a foreign key into Assigned_To, so the database
            # itself also refuses a review without an assignment.
            execute("""
                INSERT INTO Reviews (reviewer_id, paper_id, score, confidence, recommendation, comments)
                VALUES (:rid, :pid, :score, :conf, :rec, :comments)
                ON CONFLICT (reviewer_id, paper_id) DO UPDATE
                  SET score = EXCLUDED.score, confidence = EXCLUDED.confidence,
                      recommendation = EXCLUDED.recommendation, comments = EXCLUDED.comments
            """, rid=rid, pid=pid, score=form_int("score"), conf=form_int("confidence"),
                    rec=rec, comments=form_str("comments", required=False))
            commit_or_report("Review saved.")
        return run_write(action, url_for("reviewer_home", rid=rid))

    review = query_one("SELECT * FROM Reviews WHERE reviewer_id = :rid AND paper_id = :pid", rid=rid, pid=pid)
    return render_template("review_form.html", person=person, a=assignment, review=review,
                           recommendations=RECOMMENDATIONS)


# =====================================================================
# Organizer view
# =====================================================================

@app.route("/organizer")
def organizer_home():
    conferences = query("""
        SELECT c.conference_id, c.name, c.year, c.deadline,
               COUNT(p.paper_id) AS papers,
               COUNT(d.paper_id) FILTER (WHERE d.outcome = 'accept') AS accepted,
               COUNT(d.paper_id) FILTER (WHERE d.outcome = 'reject') AS rejected,
               COUNT(p.paper_id) - COUNT(d.paper_id) AS pending
        FROM Conferences c
        LEFT JOIN Papers p    ON p.conference_id = c.conference_id
        LEFT JOIN Decisions d ON d.paper_id = p.paper_id
        GROUP BY c.conference_id
        ORDER BY c.year DESC, c.name
    """)
    # Acceptance ratio per topic (one of the example queries in our proposal).
    topic_stats = query("""
        SELECT t.topic_name,
               COUNT(ht.paper_id) AS papers,
               COUNT(d.paper_id) FILTER (WHERE d.outcome = 'accept') AS accepted,
               COUNT(d.paper_id) AS decided,
               ROUND(100.0 * COUNT(d.paper_id) FILTER (WHERE d.outcome = 'accept')
                     / NULLIF(COUNT(d.paper_id), 0), 1) AS accept_pct
        FROM Topics t
        LEFT JOIN Has_Topic ht ON ht.topic_id = t.topic_id
        LEFT JOIN Decisions d  ON d.paper_id = ht.paper_id
        GROUP BY t.topic_id
        ORDER BY papers DESC, t.topic_name
    """)
    return render_template("organizer_home.html", conferences=conferences, topic_stats=topic_stats)


@app.route("/organizer/conference/<int:cid>")
def organizer_conference(cid):
    conf = query_one("SELECT * FROM Conferences WHERE conference_id = :cid", cid=cid)
    if conf is None:
        abort(404)
    papers = query("""
        SELECT p.paper_id, p.title, p.submitted_on, (p.submitted_on > c.deadline) AS late,
               (SELECT COUNT(*) FROM Bids_On b     WHERE b.paper_id = p.paper_id) AS bids,
               (SELECT COUNT(*) FROM Assigned_To a WHERE a.paper_id = p.paper_id) AS assigned,
               (SELECT COUNT(*) FROM Reviews r     WHERE r.paper_id = p.paper_id) AS reviews,
               (SELECT ROUND(AVG(r.score), 2) FROM Reviews r WHERE r.paper_id = p.paper_id) AS avg_score,
               d.outcome
        FROM Papers p
        JOIN Conferences c   ON c.conference_id = p.conference_id
        LEFT JOIN Decisions d ON d.paper_id = p.paper_id
        WHERE p.conference_id = :cid
        ORDER BY (d.outcome IS NOT NULL), avg_score DESC NULLS LAST, p.title
    """, cid=cid)
    # Undecided papers where every assigned reviewer has submitted a review:
    # "for all assignments of this paper, a review exists" (division).
    ready = query("""
        SELECT p.paper_id, p.title, COUNT(r.reviewer_id) AS reviews, ROUND(AVG(r.score), 2) AS avg_score
        FROM Papers p
        JOIN Reviews r ON r.paper_id = p.paper_id
        WHERE p.conference_id = :cid
          AND NOT EXISTS (SELECT 1 FROM Decisions d WHERE d.paper_id = p.paper_id)
          AND NOT EXISTS (SELECT 1 FROM Assigned_To a
                          WHERE a.paper_id = p.paper_id
                            AND NOT EXISTS (SELECT 1 FROM Reviews r2
                                            WHERE r2.paper_id = a.paper_id AND r2.reviewer_id = a.reviewer_id))
        GROUP BY p.paper_id
        ORDER BY avg_score DESC
    """, cid=cid)
    # Reviewer workload within this conference.
    workload = query("""
        SELECT pe.person_id, pe.name, pe.affiliation,
               COUNT(a.paper_id) AS assigned, COUNT(r.paper_id) AS reviewed
        FROM Reviewers rw
        JOIN People pe ON pe.person_id = rw.person_id
        JOIN Assigned_To a ON a.reviewer_id = rw.person_id
        JOIN Papers p ON p.paper_id = a.paper_id AND p.conference_id = :cid
        LEFT JOIN Reviews r ON r.reviewer_id = a.reviewer_id AND r.paper_id = a.paper_id
        GROUP BY pe.person_id
        ORDER BY assigned DESC, pe.name
    """, cid=cid)
    return render_template("organizer_conference.html", conf=conf, papers=papers, ready=ready, workload=workload)


@app.route("/organizer/paper/<int:pid>")
def organizer_paper(pid):
    p = query_one("""
        SELECT p.*, c.name AS conf_name, c.year
        FROM Papers p JOIN Conferences c ON c.conference_id = p.conference_id
        WHERE p.paper_id = :pid
    """, pid=pid)
    if p is None:
        abort(404)
    authors = query("""
        SELECT pe.name FROM Writes w JOIN People pe ON pe.person_id = w.author_id
        WHERE w.paper_id = :pid ORDER BY pe.name
    """, pid=pid)
    bids = query("""
        SELECT pe.person_id, pe.name, pe.affiliation, b.bid_value,
               EXISTS (SELECT 1 FROM Assigned_To a WHERE a.paper_id = b.paper_id AND a.reviewer_id = b.reviewer_id) AS assigned
        FROM Bids_On b JOIN People pe ON pe.person_id = b.reviewer_id
        WHERE b.paper_id = :pid
        ORDER BY b.bid_value DESC, pe.name
    """, pid=pid)
    reviews = query("""
        SELECT pe.person_id, pe.name, a.assigned_on, a.due_on,
               r.score, r.confidence, r.recommendation, r.comments
        FROM Assigned_To a
        JOIN People pe ON pe.person_id = a.reviewer_id
        LEFT JOIN Reviews r ON r.reviewer_id = a.reviewer_id AND r.paper_id = a.paper_id
        WHERE a.paper_id = :pid
        ORDER BY pe.name
    """, pid=pid)
    summary = query_one("""
        SELECT COUNT(*) AS n, ROUND(AVG(score), 2) AS avg_score,
               ROUND(SUM(score * confidence)::numeric / NULLIF(SUM(confidence), 0), 2) AS weighted,
               COUNT(*) FILTER (WHERE recommendation IN ('strong accept', 'accept', 'weak accept')) AS positive
        FROM Reviews WHERE paper_id = :pid
    """, pid=pid)
    # Reviewer suggestions (as proposed: based on bid value and how many
    # papers each reviewer already has). Excludes reviewers already assigned
    # and reviewers who wrote the paper.
    suggestions = query("""
        SELECT pe.person_id, pe.name, pe.affiliation, b.bid_value,
               (SELECT COUNT(*) FROM Assigned_To a2 WHERE a2.reviewer_id = rw.person_id) AS load
        FROM Reviewers rw
        JOIN People pe ON pe.person_id = rw.person_id
        LEFT JOIN Bids_On b ON b.reviewer_id = rw.person_id AND b.paper_id = :pid
        WHERE NOT EXISTS (SELECT 1 FROM Assigned_To a WHERE a.reviewer_id = rw.person_id AND a.paper_id = :pid)
          AND NOT EXISTS (SELECT 1 FROM Writes w WHERE w.author_id = rw.person_id AND w.paper_id = :pid)
        ORDER BY b.bid_value DESC NULLS LAST, load ASC, pe.name
    """, pid=pid)
    decision = query_one("SELECT * FROM Decisions WHERE paper_id = :pid", pid=pid)
    return render_template("organizer_paper.html", p=p, authors=authors, bids=bids, reviews=reviews,
                           summary=summary, suggestions=suggestions, decision=decision,
                           default_due=date.today() + timedelta(days=21))


@app.route("/organizer/paper/<int:pid>/assign", methods=["POST"])
def organizer_assign(pid):
    def action():
        rid = form_int("reviewer_id")
        due = form_date("due_on", default=date.today() + timedelta(days=21))
        # Authors cannot review their own papers. Our schema has no trigger
        # for this, so the INSERT ... SELECT ... WHERE NOT EXISTS checks
        # Writes and inserts in a single statement.
        result = execute("""
            INSERT INTO Assigned_To (reviewer_id, paper_id, assigned_on, due_on)
            SELECT :rid, :pid, CURRENT_DATE, :due
            WHERE NOT EXISTS (SELECT 1 FROM Writes w WHERE w.author_id = :rid AND w.paper_id = :pid)
        """, rid=rid, pid=pid, due=due)
        if result.rowcount == 0:
            g.conn.rollback()
            raise FormError("That reviewer is an author of this paper and cannot review it.")
        commit_or_report("Reviewer assigned.")

    return run_write(action, url_for("organizer_paper", pid=pid))


@app.route("/organizer/paper/<int:pid>/unassign", methods=["POST"])
def organizer_unassign(pid):
    def action():
        # Deleting the assignment also deletes its review (ON DELETE CASCADE).
        execute("DELETE FROM Assigned_To WHERE reviewer_id = :rid AND paper_id = :pid",
                rid=form_int("reviewer_id"), pid=pid)
        commit_or_report("Assignment removed.")

    return run_write(action, url_for("organizer_paper", pid=pid))


@app.route("/organizer/paper/<int:pid>/decide", methods=["POST"])
def organizer_decide(pid):
    def action():
        outcome = form_str("outcome")
        if outcome not in ("accept", "reject"):
            raise FormError("Outcome must be accept or reject.")
        lock("Decisions")
        # paper_id is UNIQUE in Decisions (at most one decision per paper),
        # so deciding again updates the existing decision.
        execute("""
            INSERT INTO Decisions (decision_id, outcome, comments, decided_on, paper_id)
            SELECT COALESCE(MAX(decision_id), 0) + 1, :outcome, :comments, CURRENT_DATE, :pid
            FROM Decisions
            ON CONFLICT (paper_id) DO UPDATE
              SET outcome = EXCLUDED.outcome, comments = EXCLUDED.comments, decided_on = EXCLUDED.decided_on
        """, outcome=outcome, comments=form_str("comments", required=False), pid=pid)
        commit_or_report("Decision recorded.")

    return run_write(action, url_for("organizer_paper", pid=pid))


# =====================================================================
# Manage: people (with Author / Reviewer roles), conferences, topics
# =====================================================================

@app.route("/manage")
def manage():
    people = query("""
        SELECT pe.person_id, pe.name, pe.email, pe.affiliation,
               EXISTS (SELECT 1 FROM Authors a   WHERE a.person_id = pe.person_id) AS is_author,
               EXISTS (SELECT 1 FROM Reviewers r WHERE r.person_id = pe.person_id) AS is_reviewer
        FROM People pe ORDER BY pe.name
    """)
    return render_template("manage.html", people=people, conferences=all_conferences(), topics=all_topics())


@app.route("/manage/person", methods=["POST"])
def add_person():
    def action():
        roles = request.form.getlist("roles")
        if not roles:
            raise FormError("A person needs at least one role (author and/or reviewer).")
        lock("People")
        pid = execute("""
            INSERT INTO People (person_id, name, email, affiliation)
            SELECT COALESCE(MAX(person_id), 0) + 1, :name, :email, :aff FROM People
            RETURNING person_id
        """, name=form_str("name"), email=form_str("email"), aff=form_str("affiliation", required=False)).scalar_one()
        # Authors and Reviewers are overlapping ISA subclasses of People.
        if "author" in roles:
            execute("INSERT INTO Authors (person_id) VALUES (:pid)", pid=pid)
        if "reviewer" in roles:
            execute("INSERT INTO Reviewers (person_id) VALUES (:pid)", pid=pid)
        commit_or_report("Person added.")

    return run_write(action, url_for("manage"))


@app.route("/manage/role", methods=["POST"])
def add_role():
    def action():
        pid = form_int("person_id")
        role = form_str("role")
        if role == "author":
            execute("INSERT INTO Authors (person_id) VALUES (:pid) ON CONFLICT DO NOTHING", pid=pid)
        elif role == "reviewer":
            execute("INSERT INTO Reviewers (person_id) VALUES (:pid) ON CONFLICT DO NOTHING", pid=pid)
        else:
            raise FormError("Unknown role.")
        commit_or_report("Role added.")

    return run_write(action, url_for("manage"))


@app.route("/manage/conference", methods=["POST"])
def add_conference():
    def action():
        deadline = form_date("deadline")
        if deadline is None:
            raise FormError("Please fill in the deadline.")
        lock("Conferences")
        execute("""
            INSERT INTO Conferences (conference_id, name, year, deadline)
            SELECT COALESCE(MAX(conference_id), 0) + 1, :name, :year, :deadline FROM Conferences
        """, name=form_str("name"), year=form_int("year"), deadline=deadline)
        commit_or_report("Conference added.")

    return run_write(action, url_for("manage"))


@app.route("/manage/topic", methods=["POST"])
def add_topic():
    def action():
        lock("Topics")
        execute("""
            INSERT INTO Topics (topic_id, topic_name)
            SELECT COALESCE(MAX(topic_id), 0) + 1, :name FROM Topics
        """, name=form_str("topic_name"))
        commit_or_report("Topic added.")

    return run_write(action, url_for("manage"))


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", message="Page not found."), 404


@app.errorhandler(DBAPIError)
def database_error(e):
    """A query failed (e.g. a number in the URL too large for an INTEGER
    column). Roll back and show a friendly page instead of a crash."""
    app.logger.warning("Query failed (%s)", type(e).__name__)
    try:
        if g.get("conn") is not None:
            g.conn.rollback()
    except SQLAlchemyError:
        pass
    return render_template("error.html", message="That request could not be completed. Please check your input."), 400


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", message="Something went wrong. Please go back and try again."), 500


if __name__ == "__main__":
    # Local development only (Flask loads .env here). Cloud Run uses the Procfile.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8111")))
