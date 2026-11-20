# COMS W4111 Project 1, Part 3: Conference Peer Review System

Team: Cecilia Yang (zy2644), Ziyue (Toby) Fang (zf2389)

## 1. PostgreSQL account

`zy2644` (database `proj1part2`). This is the same database we used for Part 2.

## 2. Web application URL

https://project1-web-233380246940.us-east1.run.app

## 3. What we implemented

We built everything in our Part 1 proposal. The app has four views. There
is no login. On the home page, you pick an author or reviewer by name.

- **Public:** search accepted papers by title, author, topic and
  conference, and see each author's accepted papers.
- **Author:** see your own papers and their status (under review, accepted
  or rejected), and submit a new paper with topics and co-authors.
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
papers that have at least one assigned reviewer, where *every* assigned
reviewer has turned in a review. This is a "for all" (division) query
written with two nested `NOT EXISTS`. It checks that the paper has no
assignment without a review. The page also shows each reviewer's workload
in that conference (assigned vs. reviewed). The conference ID in the URL is
the input to all of these queries.

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

## 5. Notes on the data

Paper titles, abstracts, author names and original topic labels come from
OpenReview, covering ICLR 2024/2025 and NeurIPS 2024/2025. Most reviews and
decisions are also imported. The original 20-paper dataset contains 60
generated reviews.

- All 2024 and 2025 papers are decided, as they would be today. To show
  reviewing in progress, a generated conference (Sample Peer Review
  Conference 2027) has 40 generated papers at four stages, 10 each: no
  reviewers yet, waiting for reviews, partly reviewed, and ready for a
  decision.
- NeurIPS 2025 ratings (1-6) were converted to a 1-10 range. The original
  ratings are kept in the review comments, e.g.
  `[original rating 5/6; score=8/10]`.
- Assignment dates and reviewer bids are generated sample data.
- Sixteen papers have two relevant topics to demonstrate the many-to-many
  paper–topic relationship.
- Most imported reviewer names are paper-specific anonymous labels
  (e.g. `ICLR 2024 #9310 Reviewer_BqAx`). Emails are generated sample
  addresses. Affiliations we could not find are left blank. The sample
  authors and sample reviewers (e.g. `Sample Author 01`, `Grace Thompson`)
  are fictional people with sample affiliations.
- Topic names keep each conference's own wording, so related subjects can
  appear under different names (e.g. `reinforcement learning` from ICLR and
  `reinforcement_learning` from NeurIPS).

## 6. AI tools

We used Claude for writing and reviewing the Flask routes, SQL queries and
HTML templates, the page design (CSS and layout), and testing the site and
fixing the bugs that testing found. We also used ChatGPT to prepare the
OpenReview data imports, generate sample bids, normalize review scores,
audit the database snapshot and pick the additional topics.

We reviewed the code, loaded and fixed our data in PostgreSQL ourselves,
and tested the deployed site with that data.
