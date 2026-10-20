-- =============================================================================
-- 001_booking_functions.sql
-- LangGraph workflow support functions.
--
-- The base dump (000_base_schema.sql) only ships three functions:
--   register_incoming_message, get_booking_policies, list_venues.
-- The LangFlow agents also needed availability + transactional booking logic.
-- This migration adds them, written against the existing schema.
--
-- Safe to run multiple times (idempotent).
-- =============================================================================

-- Ensure the dedupe key relied on by register_incoming_message() exists.
CREATE UNIQUE INDEX IF NOT EXISTS booking_requests_provider_msg_uidx
    ON public.booking_requests (provider, provider_message_id);

-- The exclusion constraint below uses `uuid WITH =`, which needs btree_gist.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- A court can never have two active (held/confirmed) bookings at the same time.
-- This is the hard guarantee behind the soft overlap check below.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'bookings_no_overlap'
    ) THEN
        ALTER TABLE public.bookings
            ADD CONSTRAINT bookings_no_overlap
            EXCLUDE USING gist (
                court_id WITH =,
                tstzrange(starts_at, ends_at, '[)') WITH &&
            )
            WHERE (status IN ('held', 'confirmed'));
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- check_availability: read-only availability lookup.
-- Returns available slots for a sport in a city (optionally a specific venue).
-- When p_prefer_start (HH:MM) is given, slots closest to that time are returned
-- first, so the caller can simply pick the head of the list.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.check_availability(
    p_sport        text,
    p_city         text DEFAULT NULL,
    p_venue        text DEFAULT NULL,
    p_date         date DEFAULT NULL,
    p_duration_min integer DEFAULT 60,
    p_limit        integer DEFAULT 20,
    p_prefer_start text DEFAULT NULL
) RETURNS jsonb
    LANGUAGE plpgsql
    STABLE
    SET search_path TO 'pg_catalog', 'public'
AS $$
DECLARE
    v_date        date  := COALESCE(p_date, (timezone('Africa/Casablanca', now()))::date);
    v_duration    integer := GREATEST(COALESCE(p_duration_min, 60), 15);
    v_day_start   timestamptz;
    v_day_end     timestamptz;
    v_pref_min    integer := NULL;
    v_result      jsonb;
BEGIN
    -- Parse a preferred start time (HH:MM or HH:MM:SS) into minutes-of-day.
    IF nullif(btrim(p_prefer_start), '') IS NOT NULL THEN
        BEGIN
            v_pref_min := (EXTRACT(hour FROM btrim(p_prefer_start)::time) * 60
                           + EXTRACT(minute FROM btrim(p_prefer_start)::time))::integer;
        EXCEPTION WHEN others THEN
            v_pref_min := NULL;  -- unparseable -> order by time of day instead
        END;
    END IF;

    -- Build the local working-day window in Casablanca time, as absolute instants.
    v_day_start := (v_date + interval '8 hours')  AT TIME ZONE 'Africa/Casablanca';
    v_day_end   := (v_date + interval '23 hours') AT TIME ZONE 'Africa/Casablanca';

    WITH matching_courts AS (
        SELECT c.id AS court_id, c.name AS court_name, c.price_per_hour, c.currency,
               v.name AS venue_name, v.city, s.name AS sport_name
        FROM public.courts c
        JOIN public.venues v ON v.id = c.venue_id AND v.active
        JOIN public.sports s ON s.id = c.sport_id AND s.active
        WHERE c.active
          AND lower(s.name) = lower(btrim(p_sport))
          AND (nullif(btrim(p_city), '')   IS NULL OR lower(v.city) = lower(btrim(p_city)))
          AND (nullif(btrim(p_venue), '')  IS NULL OR lower(v.name) = lower(btrim(p_venue)))
    ),
    -- Candidate start times every 30 minutes across the working day.
    candidate_starts AS (
        SELECT generate_series(
            v_day_start,
            v_day_end - make_interval(mins => v_duration),
            interval '30 minutes'
        ) AS starts_at
    ),
    busy AS (
        SELECT b.court_id, tstzrange(b.starts_at, b.ends_at, '[)') AS busy_range
        FROM public.bookings b
        WHERE b.status IN ('held', 'confirmed')
          AND b.ends_at > v_day_start
          AND b.starts_at < v_day_end
    )
    SELECT COALESCE(
        jsonb_agg(slot ORDER BY pref_dist, slot->>'starts_at', slot->>'venue_name'),
        '[]'::jsonb
    )
    INTO v_result
    FROM (
        SELECT jsonb_build_object(
            'court_id',      mc.court_id,
            'court_name',    mc.court_name,
            'venue_name',    mc.venue_name,
            'city',          mc.city,
            'sport',         mc.sport_name,
            'starts_at',     to_char(cs.starts_at AT TIME ZONE 'Africa/Casablanca', 'YYYY-MM-DD HH24:MI'),
            'ends_at',       to_char((cs.starts_at + make_interval(mins => v_duration)) AT TIME ZONE 'Africa/Casablanca', 'YYYY-MM-DD HH24:MI'),
            'duration_min',  v_duration,
            'price_per_hour', mc.price_per_hour,
            'currency',      mc.currency
        ) AS slot,
        -- Minutes between this slot's local start and the preferred time (0 if none).
        (CASE
            WHEN v_pref_min IS NULL THEN 0
            ELSE abs( ((EXTRACT(hour FROM (cs.starts_at AT TIME ZONE 'Africa/Casablanca')) * 60
                        + EXTRACT(minute FROM (cs.starts_at AT TIME ZONE 'Africa/Casablanca')))::integer) - v_pref_min )
        END)::integer AS pref_dist,
        cs.starts_at AS raw_start
        FROM matching_courts mc
        CROSS JOIN candidate_starts cs
        WHERE cs.starts_at >= now()   -- no past slots
          AND NOT EXISTS (
              SELECT 1 FROM busy b
              WHERE b.court_id = mc.court_id
                AND b.busy_range && tstzrange(cs.starts_at, cs.starts_at + make_interval(mins => v_duration), '[)')
          )
        -- Keep only the N slots closest to the preferred time (or earliest of day).
        ORDER BY pref_dist, cs.starts_at, mc.venue_name
        LIMIT GREATEST(COALESCE(p_limit, 20), 1)
    ) ranked;

    RETURN COALESCE(v_result, '[]'::jsonb);
END;
$$;

-- -----------------------------------------------------------------------------
-- hold_booking: create a time-limited hold. Idempotent via idempotency_key.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.hold_booking(
    p_court_id       uuid,
    p_sender_phone   text,
    p_starts_at      timestamptz,
    p_ends_at        timestamptz,
    p_idempotency_key text,
    p_hold_minutes   integer DEFAULT 10
) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
AS $$
DECLARE
    v_court        public.courts%ROWTYPE;
    v_booking      public.bookings%ROWTYPE;
    v_hours        numeric;
    v_price        numeric;
    v_code         text;
    v_hold         interval := make_interval(mins => COALESCE(p_hold_minutes, 10));
BEGIN
    IF p_ends_at <= p_starts_at THEN
        RETURN jsonb_build_object('status', 'invalid', 'error', 'ends_at must be after starts_at');
    END IF;

    -- Idempotency: if this exact operation already happened, return the existing hold.
    SELECT * INTO v_booking FROM public.bookings WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN jsonb_build_object(
            'status', 'idempotent', 'booking_id', v_booking.id,
            'booking_code', v_booking.booking_code, 'booking_status', v_booking.status,
            'price_total', v_booking.price_total, 'currency', v_booking.currency
        );
    END IF;

    SELECT * INTO v_court FROM public.courts WHERE id = p_court_id AND active;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'invalid', 'error', 'Court not found or inactive');
    END IF;

    -- Serialize concurrent holds on the same court to avoid double-booking races.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_court_id::text, 0));

    -- Hard conflict check (also protected by the bookings_no_overlap exclusion constraint).
    IF EXISTS (
        SELECT 1 FROM public.bookings b
        WHERE b.court_id = p_court_id
          AND b.status IN ('held', 'confirmed')
          AND tstzrange(b.starts_at, b.ends_at, '[)') && tstzrange(p_starts_at, p_ends_at, '[)')
    ) THEN
        RETURN jsonb_build_object('status', 'conflict', 'error', 'Slot already booked');
    END IF;

    v_hours := EXTRACT(epoch FROM (p_ends_at - p_starts_at)) / 3600.0;
    v_price := round(v_court.price_per_hour * v_hours, 2);
    v_code  := upper(substr(md5(random()::text || p_idempotency_key), 1, 8));

    INSERT INTO public.bookings (
        booking_code, court_id, sender_phone, starts_at, ends_at,
        status, hold_expires_at, idempotency_key, price_total, currency
    ) VALUES (
        v_code, p_court_id, p_sender_phone, p_starts_at, p_ends_at,
        'held', now() + v_hold, p_idempotency_key, v_price, v_court.currency
    )
    RETURNING * INTO v_booking;

    RETURN jsonb_build_object(
        'status', 'held', 'booking_id', v_booking.id, 'booking_code', v_booking.booking_code,
        'price_total', v_booking.price_total, 'currency', v_booking.currency,
        'hold_expires_at', to_char(v_booking.hold_expires_at AT TIME ZONE 'Africa/Casablanca', 'YYYY-MM-DD HH24:MI'),
        'hold_minutes', COALESCE(p_hold_minutes, 10)
    );
END;
$$;

-- -----------------------------------------------------------------------------
-- confirm_booking: convert a live hold (or re-confirm) to confirmed. Idempotent.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.confirm_booking(
    p_idempotency_key text,
    p_sender_phone    text
) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
AS $$
DECLARE
    v_booking public.bookings%ROWTYPE;
BEGIN
    SELECT * INTO v_booking
    FROM public.bookings
    WHERE idempotency_key = p_idempotency_key
      AND sender_phone = p_sender_phone
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found', 'error', 'No booking matches that reference');
    END IF;

    IF v_booking.status = 'confirmed' THEN
        RETURN jsonb_build_object('status', 'already_confirmed', 'booking_code', v_booking.booking_code);
    END IF;

    IF v_booking.status IN ('cancelled', 'expired') THEN
        RETURN jsonb_build_object('status', 'unavailable', 'error', 'Booking is ' || v_booking.status);
    END IF;

    IF v_booking.hold_expires_at IS NOT NULL AND v_booking.hold_expires_at < now() THEN
        UPDATE public.bookings SET status = 'expired', updated_at = now()
        WHERE id = v_booking.id;
        RETURN jsonb_build_object('status', 'expired', 'error', 'The hold has expired');
    END IF;

    UPDATE public.bookings
    SET status = 'confirmed', hold_expires_at = NULL, updated_at = now()
    WHERE id = v_booking.id
    RETURNING * INTO v_booking;

    RETURN jsonb_build_object(
        'status', 'confirmed', 'booking_code', v_booking.booking_code,
        'price_total', v_booking.price_total, 'currency', v_booking.currency,
        'starts_at', to_char(v_booking.starts_at AT TIME ZONE 'Africa/Casablanca', 'YYYY-MM-DD HH24:MI'),
        'ends_at', to_char(v_booking.ends_at AT TIME ZONE 'Africa/Casablanca', 'YYYY-MM-DD HH24:MI')
    );
END;
$$;

-- -----------------------------------------------------------------------------
-- cancel_booking: cancel a held/confirmed booking. Idempotent.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.cancel_booking(
    p_idempotency_key text,
    p_sender_phone    text
) RETURNS jsonb
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
AS $$
DECLARE
    v_booking public.bookings%ROWTYPE;
BEGIN
    SELECT * INTO v_booking
    FROM public.bookings
    WHERE idempotency_key = p_idempotency_key
      AND sender_phone = p_sender_phone
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found', 'error', 'No booking matches that reference');
    END IF;

    IF v_booking.status = 'cancelled' THEN
        RETURN jsonb_build_object('status', 'already_cancelled', 'booking_code', v_booking.booking_code);
    END IF;

    UPDATE public.bookings
    SET status = 'cancelled', hold_expires_at = NULL, updated_at = now()
    WHERE id = v_booking.id
    RETURNING * INTO v_booking;

    RETURN jsonb_build_object(
        'status', 'cancelled', 'booking_code', v_booking.booking_code
    );
END;
$$;

-- -----------------------------------------------------------------------------
-- expire_holds: release stale holds. Called periodically by the worker.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.expire_holds() RETURNS integer
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
AS $$
DECLARE
    v_count integer;
BEGIN
    WITH expired AS (
        UPDATE public.bookings
        SET status = 'expired', updated_at = now()
        WHERE status = 'held'
          AND hold_expires_at IS NOT NULL
          AND hold_expires_at < now()
        RETURNING id
    )
    SELECT count(*) INTO v_count FROM expired;
    RETURN v_count;
END;
$$;

-- -----------------------------------------------------------------------------
-- Conversation state helpers (multi-turn field collection).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_conversation_state(p_sender_phone text)
RETURNS jsonb
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'public'
AS $$
    SELECT COALESCE(
        jsonb_build_object(
            'pending_intent',   cs.pending_intent,
            'collected_fields', cs.collected_fields,
            'last_question',    cs.last_question,
            'expires_at',       cs.expires_at
        ),
        '{}'::jsonb
    )
    FROM public.conversation_state cs
    WHERE cs.sender_phone = p_sender_phone
      AND (cs.expires_at IS NULL OR cs.expires_at > now());
$$;

CREATE OR REPLACE FUNCTION public.upsert_conversation_state(
    p_sender_phone    text,
    p_pending_intent  text,
    p_collected_fields jsonb DEFAULT '{}'::jsonb,
    p_last_question   text DEFAULT NULL,
    p_ttl_minutes     integer DEFAULT 30
) RETURNS void
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
AS $$
BEGIN
    INSERT INTO public.conversation_state (sender_phone, pending_intent, collected_fields, last_question, expires_at, updated_at)
    VALUES (
        p_sender_phone, p_pending_intent, COALESCE(p_collected_fields, '{}'::jsonb),
        p_last_question, now() + make_interval(mins => COALESCE(p_ttl_minutes, 30)), now()
    )
    ON CONFLICT (sender_phone) DO UPDATE
        SET pending_intent   = EXCLUDED.pending_intent,
            collected_fields = EXCLUDED.collected_fields,
            last_question    = EXCLUDED.last_question,
            expires_at       = EXCLUDED.expires_at,
            updated_at       = now();
END;
$$;

CREATE OR REPLACE FUNCTION public.clear_conversation_state(p_sender_phone text)
RETURNS void
    LANGUAGE sql
    SET search_path TO 'pg_catalog', 'public'
AS $$
    DELETE FROM public.conversation_state WHERE sender_phone = p_sender_phone;
$$;