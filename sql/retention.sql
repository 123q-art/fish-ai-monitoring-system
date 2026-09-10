BEGIN;

-- ==========================================================
-- 1. 根据当前秒级原始数据生成分钟统计
-- ==========================================================

INSERT INTO ai_report_minute (
    camera_id,
    bucket_start,
    report_count,
    fish_avg,
    fish_min,
    fish_max,
    avg_speed_avg,
    max_speed_max,
    updated_at
)
SELECT
    camera_id,
    date_trunc('minute', received_at) AS bucket_start,

    COUNT(*) AS report_count,

    AVG(fish_count)::double precision AS fish_avg,
    MIN(fish_count) AS fish_min,
    MAX(fish_count) AS fish_max,

    AVG(
        NULLIF(
            raw_payload->'detection'->>'avg_speed_px_s',
            ''
        )::double precision
    ) AS avg_speed_avg,

    MAX(
        NULLIF(
            raw_payload->'detection'->>'max_speed_px_s',
            ''
        )::double precision
    ) AS max_speed_max,

    NOW()

FROM ai_report

-- 最近一分钟可能还没有收完整，所以暂时不汇总
WHERE received_at < date_trunc('minute', NOW())

GROUP BY
    camera_id,
    date_trunc('minute', received_at)

ON CONFLICT (camera_id, bucket_start)

DO UPDATE SET
    report_count = EXCLUDED.report_count,
    fish_avg = EXCLUDED.fish_avg,
    fish_min = EXCLUDED.fish_min,
    fish_max = EXCLUDED.fish_max,
    avg_speed_avg = EXCLUDED.avg_speed_avg,
    max_speed_max = EXCLUDED.max_speed_max,
    updated_at = NOW();


-- ==========================================================
-- 2. 根据分钟统计生成小时统计
-- ==========================================================

INSERT INTO ai_report_hour (
    camera_id,
    bucket_start,
    report_count,
    fish_avg,
    fish_min,
    fish_max,
    avg_speed_avg,
    max_speed_max,
    updated_at
)
SELECT
    camera_id,
    date_trunc('hour', bucket_start) AS bucket_start,

    SUM(report_count) AS report_count,

    CASE
        WHEN SUM(report_count) > 0
        THEN
            SUM(fish_avg * report_count)
            /
            SUM(report_count)
    END AS fish_avg,

    MIN(fish_min) AS fish_min,
    MAX(fish_max) AS fish_max,

    CASE
        WHEN SUM(report_count) FILTER (
            WHERE avg_speed_avg IS NOT NULL
        ) > 0
        THEN
            SUM(avg_speed_avg * report_count)
                FILTER (WHERE avg_speed_avg IS NOT NULL)
            /
            SUM(report_count)
                FILTER (WHERE avg_speed_avg IS NOT NULL)
    END AS avg_speed_avg,

    MAX(max_speed_max) AS max_speed_max,

    NOW()

FROM ai_report_minute

WHERE bucket_start < date_trunc('hour', NOW())

GROUP BY
    camera_id,
    date_trunc('hour', bucket_start)

ON CONFLICT (camera_id, bucket_start)

DO UPDATE SET
    report_count = EXCLUDED.report_count,
    fish_avg = EXCLUDED.fish_avg,
    fish_min = EXCLUDED.fish_min,
    fish_max = EXCLUDED.fish_max,
    avg_speed_avg = EXCLUDED.avg_speed_avg,
    max_speed_max = EXCLUDED.max_speed_max,
    updated_at = NOW();


-- ==========================================================
-- 3. 删除超过 7 天的秒级原始记录
--
-- ai_object 使用 ON DELETE CASCADE，
-- 所以对应目标记录也会一起删除
-- ==========================================================

DELETE FROM ai_report
WHERE received_at < NOW() - INTERVAL '7 days';


-- ==========================================================
-- 4. 删除超过 90 天的分钟数据
--
-- 小时数据已经生成，因此小时统计长期保存
-- ==========================================================

DELETE FROM ai_report_minute
WHERE bucket_start < NOW() - INTERVAL '90 days';


COMMIT;
