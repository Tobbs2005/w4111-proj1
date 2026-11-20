# COMS W4111 Project 1, Part 3: Conference Peer Review System

Team: Cecilia Yang (zy2644), Ziyue (Toby) Fang (zf2389)

## 1. PostgreSQL account

`zy2644`. This is the same database we used for Part 2.

## 2. Web application URL

https://project1-web-233380246940.us-east1.run.app

## 3. What we implemented

We implemented every part of our Part 1 proposal. The app has four views.
There is no login: as in our proposal, the user picks an author or reviewer
by name on the home page.

- **Public:** search accepted papers by title, author, topic and
  conference, and see each author's accepted papers.
- **Author:** see your own papers and their status (pending, accept or
  reject), and submit a new paper with topics and co-authors.
- **Reviewer:** search papers, place or change bids, see your
  assignments (overdue ones are marked), and submit or edit reviews.
- **Organizer:** paper counts and decisions for each conference,
  acceptance rate by topic, reviewer workload, and the bids and reviews for
  each paper. Organizers can assign or remove reviewers, see suggested
  reviewers, and record decisions.

New features that were not in the proposal:

- A **Manage** page to add people (as author, reviewer or both),
  conferences and topics.
- A list of papers that are **ready for a decision**, meaning every
  assigned reviewer has submitted a review.
- A **confidence-weighted** average score for each paper.

Not implemented: nothing.

## 4. Two interesting pages

**Organizer conference page (`/organizer/conference/<id>`).** Shows every
paper in a conference with its number of bids, assignments and reviews, its
average score and its decision. The most interesting query finds undecided
papers where *every* assigned reviewer has turned in a review. This is a
"for all" (division) query written with two nested `NOT EXISTS`: there is no
assignment for this paper that lacks a review. The page also shows each
reviewer's workload in that conference (assigned vs. reviewed). The
conference ID in the URL is the input to all of these queries.

**Organizer paper page (`/organizer/paper/<id>`).** Shows a paper's bids,
assignments and reviews, with average and confidence-weighted scores. It
suggests reviewers ranked by their bid on this paper, then by how many
papers they already have. Assigned reviewers and the paper's own authors are
left out. The organizer picks a reviewer from that list and a due date to
assign them. The assignment is a single `INSERT ... SELECT ... WHERE NOT
EXISTS` that refuses an author of the paper. The organizer can also record
or change the decision (`INSERT ... ON CONFLICT DO UPDATE`). This page is
interesting because it combines three relationships (Bids_On, Assigned_To,
Reviews) and enforces rules in SQL that our schema can't enforce alone.

## 5. AI tools

We used Claude (through Claude Code) for:

- writing and reviewing the Flask routes, SQL queries and HTML templates
- the page design (CSS and layout)
- checking the project against the assignment requirements (no ORM,
  bound parameters for all user input, every entity and relationship
  reachable from the UI)
- fixing bugs found while testing: friendly error pages for bad input,
  showing the decision outcome on the paper page, and a safe redirect
  after bidding
- writing this README
- building and deploying the app to Cloud Run

We reviewed the code, loaded and checked our data in PostgreSQL ourselves,
and tested the deployed site with that data.
