-- =============================================================================
-- OB Artyom Accounting Report Schema
-- Project: rbtvbsbcycdlwmrzjwun (OB Artyom)
-- Schema: public (tables + RPC functions accessing armsoft_db)
-- =============================================================================

-- 1. OB accounting company registry (what OB accounting team manages)
--    Used to find companies Artyom has but OB accounting doesn't track yet
CREATE TABLE IF NOT EXISTS public.ob_accounting_companies (
    id          BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    contract_number TEXT,
    accountant_email TEXT,
    is_active   BOOLEAN DEFAULT TRUE,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Accountant daily comment/note per working day
CREATE TABLE IF NOT EXISTS public.accountant_daily_comments (
    id               BIGSERIAL PRIMARY KEY,
    accountant_email TEXT NOT NULL,
    company_id       INTEGER,
    company_name     TEXT,
    comment_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    comment          TEXT NOT NULL,
    unaccounted_work TEXT,   -- what's not reflected in the DB / table for that day
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Grant anon role access to public tables (internal tool, no RLS needed)
GRANT SELECT, INSERT ON public.ob_accounting_companies      TO anon, authenticated;
GRANT SELECT, INSERT ON public.accountant_daily_comments    TO anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.ob_accounting_companies_id_seq   TO anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.accountant_daily_comments_id_seq TO anon, authenticated;

-- =============================================================================
-- RPC: Main accounting report (SECURITY DEFINER → can access armsoft_db)
-- =============================================================================
CREATE OR REPLACE FUNCTION public.get_accounting_report(
    p_date_from  DATE DEFAULT NULL,
    p_date_to    DATE DEFAULT NULL,
    p_accountant TEXT DEFAULT NULL
)
RETURNS TABLE (
    company_id           INTEGER,
    company_name         TEXT,
    company_code         TEXT,
    is_active            BOOLEAN,
    primary_accountant   TEXT,
    invoice_count        BIGINT,
    report_count         BIGINT,
    application_count    BIGINT,
    balance_change_count BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, armsoft_db
AS $$
BEGIN
  RETURN QUERY
  WITH
  -- Companies the specified accountant has ever worked with (for accountant filter)
  acc_companies AS (
    SELECT DISTINCT company_id
    FROM   armsoft_db.parsed_issued_invoices
    WHERE  p_accountant IS NOT NULL
      AND  creator = p_accountant
  ),

  -- Armsoft issued invoices, filtered by date (Armenia TZ) and optionally by accountant
  inv_data AS (
    SELECT company_id, COUNT(*)::BIGINT AS cnt
    FROM   armsoft_db.parsed_issued_invoices
    WHERE
      (p_date_from IS NULL OR (doc_date AT TIME ZONE 'Asia/Yerevan')::DATE >= p_date_from)
      AND (p_date_to IS NULL OR (doc_date AT TIME ZONE 'Asia/Yerevan')::DATE <= p_date_to)
      AND (p_accountant IS NULL OR creator = p_accountant)
    GROUP BY company_id
  ),

  -- TaxService submitted/archived forms (Сдано отчетности)
  rep_data AS (
    SELECT company_id, COUNT(*)::BIGINT AS cnt
    FROM   armsoft_db.tax_archive_forms
    WHERE
      (p_date_from IS NULL OR (
        submission_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        AND TO_DATE(submission_date, 'DD.MM.YYYY') >= p_date_from
      ))
      AND (p_date_to IS NULL OR (
        submission_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        AND TO_DATE(submission_date, 'DD.MM.YYYY') <= p_date_to
      ))
    GROUP BY company_id
  ),

  -- TaxService saved/prepared forms (Подано заявлений)
  app_data AS (
    SELECT company_id, COUNT(*)::BIGINT AS cnt
    FROM   armsoft_db.tax_saved_forms
    WHERE
      (p_date_from IS NULL OR (
        CASE
          WHEN created_date ~ '^\d{2}/\d{2}/\d{2}'
          THEN TO_DATE(SPLIT_PART(created_date, ' ', 1), 'DD/MM/YY')
          WHEN created_date ~ '^\d{2}\.\d{2}\.\d{4}$'
          THEN TO_DATE(created_date, 'DD.MM.YYYY')
          ELSE NULL
        END >= p_date_from
      ))
      AND (p_date_to IS NULL OR (
        CASE
          WHEN created_date ~ '^\d{2}/\d{2}/\d{2}'
          THEN TO_DATE(SPLIT_PART(created_date, ' ', 1), 'DD/MM/YY')
          WHEN created_date ~ '^\d{2}\.\d{2}\.\d{4}$'
          THEN TO_DATE(created_date, 'DD.MM.YYYY')
          ELSE NULL
        END <= p_date_to
      ))
    GROUP BY company_id
  ),

  -- TaxService unified account balance changes (Изменений остатков)
  bal_data AS (
    SELECT company_id, COUNT(*)::BIGINT AS cnt
    FROM   armsoft_db.tax_unified_account
    WHERE
      (p_date_from IS NULL OR (
        row_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        AND TO_DATE(row_date, 'DD.MM.YYYY') >= p_date_from
      ))
      AND (p_date_to IS NULL OR (
        row_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        AND TO_DATE(row_date, 'DD.MM.YYYY') <= p_date_to
      ))
    GROUP BY company_id
  ),

  -- Primary accountant per company: most frequent invoice creator (all-time, excluding shared)
  primary_acc AS (
    SELECT DISTINCT ON (company_id) company_id, creator AS accountant_email
    FROM (
      SELECT company_id, creator, COUNT(*) AS cnt
      FROM   armsoft_db.parsed_issued_invoices
      WHERE  creator NOT LIKE '% user%'
        AND  creator IS NOT NULL
        AND  creator <> ''
        AND  creator <> 'acc@onebusiness.am'
      GROUP BY company_id, creator
    ) counted
    ORDER BY company_id, cnt DESC
  )

  SELECT
    c.company_id::INTEGER,
    c.caption::TEXT                                AS company_name,
    c.name::TEXT                                   AS company_code,
    c.is_active::BOOLEAN,
    COALESCE(pa.accountant_email, 'acc@onebusiness.am')::TEXT AS primary_accountant,
    COALESCE(i.cnt, 0),
    COALESCE(r.cnt, 0),
    COALESCE(a.cnt, 0),
    COALESCE(b.cnt, 0)
  FROM   armsoft_db.armsoft_companies c
  LEFT JOIN primary_acc pa ON c.company_id = pa.company_id
  LEFT JOIN inv_data     i  ON c.company_id = i.company_id
  LEFT JOIN rep_data     r  ON c.company_id = r.company_id
  LEFT JOIN app_data     a  ON c.company_id = a.company_id
  LEFT JOIN bal_data     b  ON c.company_id = b.company_id
  WHERE
    -- Accountant filter: show only companies that accountant has worked with
    p_accountant IS NULL
    OR c.company_id IN (SELECT company_id FROM acc_companies)
  ORDER BY c.caption;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_accounting_report TO anon, authenticated;

-- =============================================================================
-- RPC: List of all accountants (based on invoice creators)
-- =============================================================================
CREATE OR REPLACE FUNCTION public.get_accountant_list()
RETURNS TABLE (
    accountant_email TEXT,
    companies_count  BIGINT,
    total_invoices   BIGINT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, armsoft_db
AS $$
  SELECT
    creator::TEXT            AS accountant_email,
    COUNT(DISTINCT company_id)::BIGINT AS companies_count,
    COUNT(*)::BIGINT         AS total_invoices
  FROM armsoft_db.parsed_issued_invoices
  WHERE creator NOT LIKE '% user%'
    AND creator IS NOT NULL
    AND creator <> ''
  GROUP BY creator
  ORDER BY companies_count DESC;
$$;

GRANT EXECUTE ON FUNCTION public.get_accountant_list TO anon, authenticated;

-- =============================================================================
-- RPC: Companies in Artyom's DB but NOT in OB accounting registry
-- =============================================================================
CREATE OR REPLACE FUNCTION public.get_missing_companies()
RETURNS TABLE (
    company_id   INTEGER,
    company_name TEXT,
    company_code TEXT,
    is_active    BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, armsoft_db
AS $$
BEGIN
  RETURN QUERY
  SELECT
    c.company_id::INTEGER,
    c.caption::TEXT AS company_name,
    c.name::TEXT    AS company_code,
    c.is_active::BOOLEAN
  FROM armsoft_db.armsoft_companies c
  WHERE NOT EXISTS (
    SELECT 1
    FROM   public.ob_accounting_companies ob
    WHERE
      LOWER(TRIM(ob.company_name)) = LOWER(TRIM(c.caption))
      OR LOWER(TRIM(ob.company_name)) = LOWER(TRIM(c.name))
  )
  ORDER BY c.caption;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_missing_companies TO anon, authenticated;

-- =============================================================================
-- RPC: Summary stats (total counts across all companies, with filters)
-- =============================================================================
CREATE OR REPLACE FUNCTION public.get_summary_stats(
    p_date_from  DATE DEFAULT NULL,
    p_date_to    DATE DEFAULT NULL,
    p_accountant TEXT DEFAULT NULL
)
RETURNS TABLE (
    total_companies     BIGINT,
    total_invoices      BIGINT,
    total_reports       BIGINT,
    total_applications  BIGINT,
    total_balance_changes BIGINT,
    missing_companies   BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, armsoft_db
AS $$
DECLARE
  v_invoices   BIGINT;
  v_reports    BIGINT;
  v_apps       BIGINT;
  v_balances   BIGINT;
  v_companies  BIGINT;
  v_missing    BIGINT;
BEGIN
  -- Invoice count
  SELECT COUNT(*) INTO v_invoices
  FROM armsoft_db.parsed_issued_invoices
  WHERE
    (p_date_from IS NULL OR (doc_date AT TIME ZONE 'Asia/Yerevan')::DATE >= p_date_from)
    AND (p_date_to IS NULL OR (doc_date AT TIME ZONE 'Asia/Yerevan')::DATE <= p_date_to)
    AND (p_accountant IS NULL OR creator = p_accountant);

  -- Report count
  SELECT COUNT(*) INTO v_reports
  FROM armsoft_db.tax_archive_forms
  WHERE
    (p_date_from IS NULL OR (
      submission_date ~ '^\d{2}\.\d{2}\.\d{4}$'
      AND TO_DATE(submission_date, 'DD.MM.YYYY') >= p_date_from
    ))
    AND (p_date_to IS NULL OR (
      submission_date ~ '^\d{2}\.\d{2}\.\d{4}$'
      AND TO_DATE(submission_date, 'DD.MM.YYYY') <= p_date_to
    ));

  -- Application count
  SELECT COUNT(*) INTO v_apps
  FROM armsoft_db.tax_saved_forms
  WHERE
    (p_date_from IS NULL OR (
      CASE
        WHEN created_date ~ '^\d{2}/\d{2}/\d{2}'
        THEN TO_DATE(SPLIT_PART(created_date, ' ', 1), 'DD/MM/YY')
        WHEN created_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        THEN TO_DATE(created_date, 'DD.MM.YYYY')
        ELSE NULL
      END >= p_date_from
    ))
    AND (p_date_to IS NULL OR (
      CASE
        WHEN created_date ~ '^\d{2}/\d{2}/\d{2}'
        THEN TO_DATE(SPLIT_PART(created_date, ' ', 1), 'DD/MM/YY')
        WHEN created_date ~ '^\d{2}\.\d{2}\.\d{4}$'
        THEN TO_DATE(created_date, 'DD.MM.YYYY')
        ELSE NULL
      END <= p_date_to
    ));

  -- Balance changes count
  SELECT COUNT(*) INTO v_balances
  FROM armsoft_db.tax_unified_account
  WHERE
    (p_date_from IS NULL OR (
      row_date ~ '^\d{2}\.\d{2}\.\d{4}$'
      AND TO_DATE(row_date, 'DD.MM.YYYY') >= p_date_from
    ))
    AND (p_date_to IS NULL OR (
      row_date ~ '^\d{2}\.\d{2}\.\d{4}$'
      AND TO_DATE(row_date, 'DD.MM.YYYY') <= p_date_to
    ));

  -- Total companies
  SELECT COUNT(*) INTO v_companies FROM armsoft_db.armsoft_companies;

  -- Missing companies count
  SELECT COUNT(*) INTO v_missing
  FROM armsoft_db.armsoft_companies c
  WHERE NOT EXISTS (
    SELECT 1 FROM public.ob_accounting_companies ob
    WHERE
      LOWER(TRIM(ob.company_name)) = LOWER(TRIM(c.caption))
      OR LOWER(TRIM(ob.company_name)) = LOWER(TRIM(c.name))
  );

  RETURN QUERY SELECT v_companies, v_invoices, v_reports, v_apps, v_balances, v_missing;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_summary_stats TO anon, authenticated;
