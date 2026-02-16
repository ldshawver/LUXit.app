-- Sample analytics dashboard queries (tenant-scoped)

-- Events by day
SELECT DATE(occurred_at) AS day, COUNT(*) AS total_events
FROM raw_events
WHERE company_id = :company_id
  AND occurred_at BETWEEN :start_dt AND :end_dt
GROUP BY DATE(occurred_at)
ORDER BY day ASC;

-- Sessions by day
SELECT DATE(started_at) AS day, COUNT(*) AS sessions
FROM sessions
WHERE company_id = :company_id
  AND started_at BETWEEN :start_dt AND :end_dt
GROUP BY DATE(started_at)
ORDER BY day ASC;

-- Top pages
SELECT page_url, COUNT(*) AS hits
FROM raw_events
WHERE company_id = :company_id
  AND occurred_at BETWEEN :start_dt AND :end_dt
  AND page_url IS NOT NULL
GROUP BY page_url
ORDER BY hits DESC
LIMIT 10;

-- Top referrers
SELECT referrer, COUNT(*) AS hits
FROM raw_events
WHERE company_id = :company_id
  AND occurred_at BETWEEN :start_dt AND :end_dt
  AND referrer IS NOT NULL
GROUP BY referrer
ORDER BY hits DESC
LIMIT 10;
