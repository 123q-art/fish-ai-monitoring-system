import os
import json
import logging

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("fish-ai-report")


# 测试阶段先使用默认值
AI_REPORT_TOKEN = os.getenv(
    "AI_REPORT_TOKEN",
    "CHANGE_ME"
)

DB_DSN = os.getenv(
    "DB_DSN",
    (
        "host=127.0.0.1 "
        "port=5432 "
        "dbname=fish_ai "
        "user=fish_ai "
        "password=CHANGE_ME"
    )
)


app = FastAPI(
    title="Fish AI Report Server",
    version="1.0"
)


class BBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class Center(BaseModel):
    x: float
    y: float


class Velocity(BaseModel):
    vx: float
    vy: float


class DetectionObject(BaseModel):
    track_id: int
    class_name: str = Field(alias="class")
    confidence: float

    bbox: BBox
    center: Center
    velocity: Velocity


class AIReport(BaseModel):
    # 保留 V6.2 发送的所有扩展字段：
    # schema_version / event_type / node_id / source /
    # video / ai / detection 等。
    model_config = ConfigDict(extra="allow")

    camera_id: str
    timestamp: float
    fish_count: int
    objects: list[DetectionObject] = Field(default_factory=list)


@app.get("/")
def root():
    return {
        "service": "fish-ai-report-server",
        "status": "running"
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.post("/api/ai/report")
def receive_ai_report(
    report: AIReport,
    x_api_token: str | None = Header(
        default=None,
        alias="X-API-Token"
    )
):
    # Token 校验
    if x_api_token != AI_REPORT_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid API token"
        )

    logger.info(
        "camera=%s fish_count=%s objects=%s timestamp=%s",
        report.camera_id,
        report.fish_count,
        len(report.objects),
        report.timestamp
    )

    payload = report.model_dump(by_alias=True)

    try:
        with psycopg.connect(DB_DSN) as conn:

            with conn.cursor() as cur:

                # 1. 摄像头不存在则创建，存在则更新时间
                cur.execute(
                    """
                    INSERT INTO camera (
                        camera_id,
                        last_seen_at
                    )
                    VALUES (
                        %s,
                        NOW()
                    )
                    ON CONFLICT (camera_id)
                    DO UPDATE SET
                        last_seen_at = NOW()
                    """,
                    (
                        report.camera_id,
                    )
                )

                # 2. 保存一次 AI 上报记录
                cur.execute(
                    """
                    INSERT INTO ai_report (
                        camera_id,
                        edge_timestamp,
                        fish_count,
                        raw_payload
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s::jsonb
                    )
                    RETURNING id
                    """,
                    (
                        report.camera_id,
                        report.timestamp,
                        report.fish_count,
                        json.dumps(
                            payload,
                            ensure_ascii=False
                        )
                    )
                )

                report_id = cur.fetchone()[0]

                # 3. 保存每一个检测目标
                for obj in report.objects:

                    cur.execute(
                        """
                        INSERT INTO ai_object (
                            report_id,
                            track_id,
                            class_name,
                            confidence,

                            x1,
                            y1,
                            x2,
                            y2,

                            center_x,
                            center_y,

                            vx,
                            vy
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s
                        )
                        """,
                        (
                            report_id,
                            obj.track_id,
                            obj.class_name,
                            obj.confidence,

                            obj.bbox.x1,
                            obj.bbox.y1,
                            obj.bbox.x2,
                            obj.bbox.y2,

                            obj.center.x,
                            obj.center.y,

                            obj.velocity.vx,
                            obj.velocity.vy
                        )
                    )

            conn.commit()

    except Exception:
        logger.exception("Database write failed")

        raise HTTPException(
            status_code=500,
            detail="Database write failed"
        )

    return {
        "code": 0,
        "message": "ok",
        "camera_id": report.camera_id,
        "record_id": report_id,
        "fish_count": report.fish_count,
        "object_count": len(report.objects)
    }


# ============================================================
# QUERY API V1
# ============================================================


@app.get("/api/cameras/{camera_id}/latest")
def get_camera_latest(camera_id: str):
    """
    获取某摄像头最新一次 AI 检测结果。
    """

    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        id,
                        camera_id,
                        edge_timestamp,
                        fish_count,
                        received_at,
                        raw_payload
                    FROM ai_report
                    WHERE camera_id = %s
                    ORDER BY received_at DESC
                    LIMIT 1
                    """,
                    (camera_id,)
                )

                row = cur.fetchone()

    except Exception:
        logger.exception("Latest query failed")
        raise HTTPException(
            status_code=500,
            detail="Database query failed"
        )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Camera data not found"
        )

    payload = row[5] or {}
    detection = payload.get("detection") or {}

    return {
        "camera_id": row[1],
        "report_id": row[0],

        "timestamp": row[2],

        "received_at": (
            row[4].isoformat()
            if row[4] is not None
            else None
        ),

        "fish_count": row[3],

        "avg_speed_px_s": detection.get(
            "avg_speed_px_s"
        ),

        "max_speed_px_s": detection.get(
            "max_speed_px_s"
        ),

        "frame_seq": detection.get(
            "frame_seq"
        ),

        "connection_id": detection.get(
            "connection_id"
        ),

        "result_age_ms": detection.get(
            "result_age_ms"
        ),

        "source_frame_age_ms": detection.get(
            "source_frame_age_ms"
        ),

        "objects": payload.get(
            "objects",
            []
        )
    }


@app.get("/api/cameras/{camera_id}/history")
def get_camera_history(
    camera_id: str,

    resolution: str = Query(
        default="minute",
        pattern="^(minute|hour)$"
    ),

    hours: int = Query(
        default=24,
        ge=1,
        le=8760
    )
):
    """
    获取趋势数据。

    resolution=minute:
        最近90天以内的分钟趋势。

    resolution=hour:
        小时趋势，可查询更长周期。

    最近7天直接从 ai_report 实时计算，
    更老的数据从聚合表读取。
    """

    if resolution == "minute":

        if hours > 2160:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Minute resolution supports "
                    "up to 2160 hours (90 days)"
                )
            )

        table_name = "ai_report_minute"
        bucket_unit = "minute"
        next_interval = "1 minute"

    else:

        table_name = "ai_report_hour"
        bucket_unit = "hour"
        next_interval = "1 hour"

    sql = f"""
        WITH old_data AS (

            SELECT
                bucket_start,
                report_count,
                fish_avg,
                fish_min,
                fish_max,
                avg_speed_avg AS avg_speed,
                max_speed_max AS max_speed

            FROM {table_name}

            WHERE camera_id = %s

              AND bucket_start >=
                  NOW() - (%s * INTERVAL '1 hour')

              AND bucket_start <
                  date_trunc(
                      '{bucket_unit}',
                      NOW() - INTERVAL '7 days'
                  )
                  + INTERVAL '{next_interval}'
        ),

        recent_data AS (

            SELECT
                date_trunc(
                    '{bucket_unit}',
                    received_at
                ) AS bucket_start,

                COUNT(*) AS report_count,

                AVG(fish_count)::double precision
                    AS fish_avg,

                MIN(fish_count)
                    AS fish_min,

                MAX(fish_count)
                    AS fish_max,

                AVG(
                    NULLIF(
                        raw_payload
                        ->'detection'
                        ->>'avg_speed_px_s',
                        ''
                    )::double precision
                ) AS avg_speed,

                MAX(
                    NULLIF(
                        raw_payload
                        ->'detection'
                        ->>'max_speed_px_s',
                        ''
                    )::double precision
                ) AS max_speed

            FROM ai_report

            WHERE camera_id = %s

              AND received_at >= GREATEST(

                  NOW()
                  - (%s * INTERVAL '1 hour'),

                  date_trunc(
                      '{bucket_unit}',
                      NOW() - INTERVAL '7 days'
                  )
                  + INTERVAL '{next_interval}'
              )

            GROUP BY
                date_trunc(
                    '{bucket_unit}',
                    received_at
                )
        )

        SELECT *
        FROM (

            SELECT * FROM old_data

            UNION ALL

            SELECT * FROM recent_data

        ) AS history

        ORDER BY bucket_start ASC
    """

    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:

                cur.execute(
                    sql,
                    (
                        camera_id,
                        hours,
                        camera_id,
                        hours,
                    )
                )

                rows = cur.fetchall()

    except Exception:
        logger.exception("History query failed")
        raise HTTPException(
            status_code=500,
            detail="Database query failed"
        )

    points = []

    for row in rows:

        points.append(
            {
                "time": (
                    row[0].isoformat()
                    if row[0] is not None
                    else None
                ),

                "report_count": int(
                    row[1] or 0
                ),

                "fish_avg": (
                    round(float(row[2]), 2)
                    if row[2] is not None
                    else None
                ),

                "fish_min": row[3],
                "fish_max": row[4],

                "avg_speed_px_s": (
                    round(float(row[5]), 2)
                    if row[5] is not None
                    else None
                ),

                "max_speed_px_s": (
                    round(float(row[6]), 2)
                    if row[6] is not None
                    else None
                ),
            }
        )

    return {
        "camera_id": camera_id,
        "resolution": resolution,
        "hours": hours,
        "count": len(points),
        "points": points
    }


@app.get("/api/cameras/{camera_id}/statistics")
def get_camera_statistics(
    camera_id: str,

    hours: int = Query(
        default=24,
        ge=1,
        le=168
    )
):
    """
    基于最近7天秒级原始数据计算总体统计。
    """

    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        COUNT(*),

                        AVG(fish_count),
                        MIN(fish_count),
                        MAX(fish_count),

                        AVG(
                            NULLIF(
                                raw_payload
                                ->'detection'
                                ->>'avg_speed_px_s',
                                ''
                            )::double precision
                        ),

                        MAX(
                            NULLIF(
                                raw_payload
                                ->'detection'
                                ->>'max_speed_px_s',
                                ''
                            )::double precision
                        ),

                        MIN(received_at),
                        MAX(received_at)

                    FROM ai_report

                    WHERE camera_id = %s

                      AND received_at >=
                          NOW()
                          - (%s * INTERVAL '1 hour')
                    """,
                    (
                        camera_id,
                        hours
                    )
                )

                row = cur.fetchone()

    except Exception:
        logger.exception("Statistics query failed")
        raise HTTPException(
            status_code=500,
            detail="Database query failed"
        )

    if row is None or row[0] == 0:

        raise HTTPException(
            status_code=404,
            detail="Camera data not found"
        )

    return {
        "camera_id": camera_id,
        "hours": hours,

        "report_count": int(
            row[0]
        ),

        "fish_avg": (
            round(float(row[1]), 2)
            if row[1] is not None
            else None
        ),

        "fish_min": row[2],
        "fish_max": row[3],

        "avg_speed_px_s": (
            round(float(row[4]), 2)
            if row[4] is not None
            else None
        ),

        "max_speed_px_s": (
            round(float(row[5]), 2)
            if row[5] is not None
            else None
        ),

        "start_time": (
            row[6].isoformat()
            if row[6] is not None
            else None
        ),

        "end_time": (
            row[7].isoformat()
            if row[7] is not None
            else None
        )
    }



# ============================================================
# CAMERA LIST API V1
# ============================================================

@app.get("/api/cameras")
@app.get("/api/cameras/")
def get_cameras():
    """
    获取已经注册并配置视频流的摄像头列表。
    """

    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        camera_id,
                        camera_name,
                        node_id,
                        app,
                        stream_name,
                        location,
                        enabled,
                        created_at,
                        last_seen_at,

                        CASE
                            WHEN last_seen_at IS NOT NULL
                             AND last_seen_at >= NOW() - INTERVAL '10 seconds'
                            THEN TRUE
                            ELSE FALSE
                        END AS ai_online

                    FROM camera

                    WHERE enabled = TRUE
                      AND stream_name IS NOT NULL
                      AND stream_name <> ''

                    ORDER BY camera_id
                    """
                )

                rows = cur.fetchall()

    except Exception:
        logger.exception("Camera list query failed")
        raise HTTPException(
            status_code=500,
            detail="Database query failed"
        )

    cameras = []

    for row in rows:

        camera_id = row[0]
        app = row[3] or "live"
        stream_name = row[4]

        cameras.append(
            {
                "camera_id": camera_id,

                "camera_name": (
                    row[1]
                    or camera_id
                ),

                "node_id": row[2],

                "app": app,

                "stream_name": stream_name,

                "location": row[5],

                "enabled": row[6],

                "created_at": (
                    row[7].isoformat()
                    if row[7] is not None
                    else None
                ),

                "last_seen_at": (
                    row[8].isoformat()
                    if row[8] is not None
                    else None
                ),

                "ai_online": row[9],

                "video_url":
                    "/video/"
                    + stream_name
                    + ".flv",

                "latest_url":
                    "/api/cameras/"
                    + camera_id
                    + "/latest",

                "history_url":
                    "/api/cameras/"
                    + camera_id
                    + "/history",

                "statistics_url":
                    "/api/cameras/"
                    + camera_id
                    + "/statistics"
            }
        )

    return {
        "count": len(cameras),
        "cameras": cameras
    }

