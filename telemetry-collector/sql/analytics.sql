-- DocTheCode product analytics (Postgres). Run in the Neon/Supabase SQL editor, or point
-- Metabase / Grafana / Superset at the database and paste these as saved questions.
-- Every user id is a per-server hash: you can count and follow users, never identify them.

-- ------------------------------------------------------------------ adoption
-- Active servers and their versions (who should upgrade?)
SELECT version, count(*) AS servers, max(last_seen) AS last_seen
FROM instances WHERE last_seen > now() - interval '7 days'
GROUP BY version ORDER BY servers DESC;

-- Daily active users across all servers
SELECT date_trunc('day', ts) AS day, count(DISTINCT instance_id || ':' || user_hash) AS active_users,
       count(DISTINCT instance_id) AS active_servers
FROM events WHERE user_hash IS NOT NULL AND ts > now() - interval '30 days'
GROUP BY 1 ORDER BY 1;

-- Install size distribution (latest heartbeat per server)
SELECT DISTINCT ON (instance_id) instance_id, ts, props->>'users' AS users, props->>'doc_repos' AS doc_repos,
       props->>'connected_repos' AS connected_repos, (props->>'enterprise_hosts')::int AS enterprise_hosts
FROM events WHERE name = 'instance_heartbeat'
ORDER BY instance_id, ts DESC;

-- ------------------------------------------------------------------ onboarding funnel
-- How far does each new server get? Drop-offs tell you which setup step to improve.
WITH firsts AS (
  SELECT instance_id, name, min(ts) AS at FROM events
  WHERE name IN ('server_started', 'setup_unlocked', 'github_app_saved', 'login', 'repo_created',
                 'repo_linked', 'doc_pr_opened', 'agent_run', 'review_submitted', 'doc_pr_merged')
  GROUP BY 1, 2)
SELECT name AS step, count(*) AS servers,
       round(100.0 * count(*) / max(count(*)) OVER (), 1) AS pct_of_installs,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM at - s.started) / 3600) AS median_hours_from_install
FROM firsts f JOIN (SELECT instance_id, min(ts) AS started FROM events GROUP BY 1) s USING (instance_id)
GROUP BY name ORDER BY servers DESC;

-- GitHub App setup: one-click manifest vs manual credentials
SELECT props->>'method' AS method, (props->>'enterprise')::bool AS enterprise, count(*)
FROM events WHERE name = 'github_app_saved' GROUP BY 1, 2;

-- ------------------------------------------------------------------ the core loop
-- Doc PRs per week and what happened to them
SELECT date_trunc('week', ts) AS week,
       count(*) FILTER (WHERE name = 'doc_pr_opened') AS opened,
       count(*) FILTER (WHERE name = 'doc_pr_merged') AS merged,
       count(*) FILTER (WHERE name = 'doc_pr_merged' AND props->>'gate_passed' = 'false') AS merged_bypassing_check,
       count(*) FILTER (WHERE name = 'doc_pr_closed') AS closed
FROM events WHERE ts > now() - interval '90 days' GROUP BY 1 ORDER BY 1;

-- Agent quality by AI provider and model: finish rate, speed, how often reviewers send it back
SELECT props->>'provider' AS provider, props->>'model' AS model, count(*) AS runs,
       round(100.0 * avg(CASE WHEN props->>'status' = 'finished' THEN 1 ELSE 0 END), 1) AS pct_finished,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (props->>'seconds')::float) AS median_seconds,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY (props->>'seconds')::float) AS p90_seconds,
       avg((props->>'turns')::int) AS avg_turns,
       round(100.0 * avg(CASE WHEN (props->>'files_changed')::int = 0 THEN 1 ELSE 0 END), 1) AS pct_no_changes,
       round(100.0 * avg(CASE WHEN (props->>'feedback')::int > 0 THEN 1 ELSE 0 END), 1) AS pct_redrafts
FROM events WHERE name = 'agent_run' AND ts > now() - interval '30 days'
GROUP BY 1, 2 ORDER BY runs DESC;

-- Which AI providers do organizations choose, and do their connection tests pass?
SELECT props->>'provider' AS provider, count(DISTINCT instance_id) AS servers,
       count(*) FILTER (WHERE name = 'agent_test' AND props->>'ok' = 'false') AS failed_tests
FROM events WHERE name IN ('agent_configured', 'agent_test') GROUP BY 1 ORDER BY servers DESC;

-- Review outcomes and turnaround (minutes from doc PR opened to review)
SELECT props->>'state' AS state, count(*),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (props->>'minutes_since_opened')::float) AS median_minutes,
       round(100.0 * avg(CASE WHEN props->>'is_author' = 'true' THEN 1 ELSE 0 END), 1) AS pct_by_code_author,
       round(100.0 * avg(CASE WHEN props->>'ask_agent' = 'true' THEN 1 ELSE 0 END), 1) AS pct_sent_to_agent
FROM events WHERE name = 'review_submitted' AND ts > now() - interval '30 days'
GROUP BY 1;

-- Feature usage: which features do people actually use? (UI events are prefixed "ui.")
SELECT name, count(*) AS uses, count(DISTINCT instance_id) AS servers,
       count(DISTINCT instance_id || ':' || coalesce(user_hash, '')) AS users
FROM events WHERE ts > now() - interval '30 days' AND name NOT IN ('instance_heartbeat', 'webhook_received')
GROUP BY name ORDER BY uses DESC;

-- Most visited screens
SELECT props->>'route' AS screen, count(*) AS views, count(DISTINCT instance_id || ':' || user_hash) AS users
FROM events WHERE name = 'ui.page_view' AND ts > now() - interval '30 days'
GROUP BY 1 ORDER BY views DESC;

-- ------------------------------------------------------------------ reliability
-- Top errors (group by fingerprint), newest versions first
SELECT fingerprint, kind, route, min(message) AS example_message, count(*) AS occurrences,
       count(DISTINCT instance_id) AS servers, max(version) AS latest_version, max(ts) AS last_seen,
       (array_agg(stack ORDER BY ts DESC))[1] AS latest_stack
FROM errors WHERE ts > now() - interval '14 days'
GROUP BY fingerprint, kind, route ORDER BY occurrences DESC LIMIT 50;

-- Error rate and latency per API route
SELECT route, method, sum(count) AS calls,
       round(100.0 * sum(count) FILTER (WHERE status >= 500) / nullif(sum(count), 0), 2) AS pct_5xx,
       round(100.0 * sum(count) FILTER (WHERE status BETWEEN 400 AND 499) / nullif(sum(count), 0), 2) AS pct_4xx,
       round((sum(total_ms) / nullif(sum(count), 0))::numeric, 1) AS avg_ms, max(max_ms) AS worst_ms
FROM request_stats WHERE window_start > now() - interval '7 days'
GROUP BY route, method ORDER BY calls DESC;

-- Agent crashes by exception type
SELECT props->>'error' AS error, count(*) FROM events
WHERE name = 'agent_run' AND props->>'status' = 'crashed' GROUP BY 1 ORDER BY 2 DESC;

-- ------------------------------------------------------------------ retention policy
-- Keep 13 months of raw data. Schedule this (e.g. a daily cron / pg_cron job).
-- DELETE FROM events WHERE ts < now() - interval '13 months';
-- DELETE FROM errors WHERE ts < now() - interval '13 months';
-- DELETE FROM request_stats WHERE window_start < now() - interval '13 months';
