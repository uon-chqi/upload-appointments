"""Direct MySQL access to a facility's OpenMRS database.

Facilities are independent KenyaEMR containers, each with its own MySQL, so a
connection is opened per facility from a `FacilityConfig` rather than resolved
from a static Django `DATABASES` alias. Nothing here goes through the ORM — the
appointment query is raw SQL against a cursor either way.
"""
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

import MySQLdb
from django.conf import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
# The appointment query is slow (many correlated subqueries) but bounded; this
# only needs to be generous enough that a healthy facility never trips it.
READ_TIMEOUT = 900

# Table names are deliberately unqualified: the connected database (see
# FacilityConfig.database) supplies the schema. The query used to mix
# `openmrs.encounter` with a bare `global_property`, which already assumed the
# default schema was `openmrs` — now that assumption is explicit and per-facility.
APPOINTMENT_QUERY = """
with vls as(SELECT distinct
    e.patient_id,
    CASE
        WHEN cn2.name = 'NOT DETECTED' THEN '0.00'
        ELSE (
            CASE
                WHEN o.concept_id IN (5497,730,654,790,856) THEN o.value_numeric
                WHEN o.concept_id IN (1030,1305) THEN o.value_coded
            END
        )
    END AS TestResults,
    o.obs_datetime AS VLDate
FROM encounter e
INNER JOIN obs o
    ON e.encounter_id = o.encounter_id
    AND o.voided = 0
    AND o.concept_id IN (5497,730,654,790,856,1030,1305)
LEFT JOIN orders od
    ON od.order_id = o.order_id
    AND od.voided = 0
LEFT JOIN concept_name cn1
    ON cn1.concept_id = o.concept_id
    AND cn1.locale = 'en'
    AND cn1.concept_name_type = 'FULLY_SPECIFIED'
LEFT JOIN concept_name cn2
    ON cn2.concept_id = (
        CASE
            WHEN o.concept_id IN (1030,1305) THEN o.value_coded
        END
    )
    AND cn2.locale = 'en'
    AND cn2.concept_name_type = 'FULLY_SPECIFIED'
WHERE e.voided = 0 AND cn1.name LIKE '%%viral%%'),
cd4 as (SELECT e.patient_id,
    o.value_numeric AS TestResults,
    o.obs_datetime AS TestDate
FROM encounter e
INNER JOIN obs o
    ON e.encounter_id = o.encounter_id
    AND o.voided = 0
    AND o.concept_id = 5497
LEFT JOIN orders od
    ON od.order_id = o.order_id
    AND od.voided = 0
LEFT JOIN concept_name cn1
    ON cn1.concept_id = o.concept_id
    AND cn1.locale = 'en'
    AND cn1.concept_name_type = 'FULLY_SPECIFIED'
WHERE e.voided = 0 AND cn1.name LIKE '%%cd4%%')
select max(a.patient_id) as patient_id
, max(a.visit_date) as visit_date
, max(appointment_date) as appointment_date
, max(patient_name) as patient_name
, max(a.gender) as gender
, max(a.dob) as dob
, max(a.age) as age
, max(a.ccc_number) as ccc_number
, max(a.phone_number) as phone_number
, coalesce(max(a.risk_score), max(a.risk_classification)) as risk_classification_value
, coalesce(max(a.risk_description), (case when max(a.risk_classification) is null then 'Unknown Risk'
	when max(a.risk_classification) = 0 then 'Unknown Risk'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.lowRiskThreshold' limit 1) then 'Low Risk'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.mediumRiskThreshold' limit 1) then 'Medium Risk'
	when max(a.risk_classification) <= (select property_value from global_property where property='kenyaemrml.iit.highRiskThreshold' limit 1) then 'High Risk'
	else 'Unknown Risk'
	end)) as risk_classification
, coalesce(max(a.risk_date), max(a.risk_classification_date)) as risk_classification_date
, max(a.risk_factors) as risk_factors
, (select TestResults from vls x where x.patient_id=a.patient_id order by VLDate desc limit 1) as last_viral_load
, (select VLDate from vls x where x.patient_id=a.patient_id order by VLDate desc limit 1) as last_viral_load_date
, (select TestResults from cd4 x where x.patient_id=a.patient_id order by TestDate desc limit 1) as last_cd4
, (select TestDate from cd4 x where x.patient_id=a.patient_id order by TestDate desc limit 1) as last_cd4_date
, max(a.appointment_status) as appointment_status
, max(a.county) as county
, max(a.sub_county) as sub_county
, max(a.ward) as ward
, max(a.city_village) as city_village
, max(a.landmark) as landmark
, max(a.address5) as address5
, max(a.address6) as address6
, max(a.marital_status) as marital_status
, max(a.case_manager_assigned) as case_manager_assigned
, max(a.facility_mfl) as facility_mfl
, max(a.facility_name) as facility_name
, max(a.consented) as consented
, max(a.program) as program
, max(a.lmp) as lmp
, max(a.edd) as edd
, max(a.delivery_date) as delivery_date
, max(a.intervention_list) as intervention_list
, max(a.cormobidities) as cormobidities
, max(a.enrollment_date) as enrollment_date
, max(a.start_ART_date) as start_ART_date
, case when max(a.restart_ART_date) > max(a.start_ART_date) then max(a.restart_ART_date) else null end as restart_ART_date
from
(
    select distinct a.patient_id
    , date(a.date_started) as visit_date
    , (select x.start_date_time from patient_appointment x where x.patient_id = a.patient_id and x.status != 'Cancelled' and x.voided=0
    order by x.start_date_time desc limit 1) as appointment_date
    , (select concat(x.given_name, ' ', ifnull(x.middle_name, '') , ' ', x.family_name)
        from person_name x where x.person_id =a.patient_id limit 1) as patient_name
    , b.gender
    , b.birthdate as dob
    , round(TIMESTAMPDIFF(month, b.birthdate, now())/12.0, 0) as age
    , (select x.identifier from patient_identifier x inner join patient_identifier_type y on x.identifier_type=y.patient_identifier_type_id
    and y.name ='Unique Patient Number' where x.patient_id = a.patient_id limit 1) as ccc_number
    , (select x.value from person_attribute x inner join person_attribute_type y on x.person_attribute_type_id =y.person_attribute_type_id
    and y.name='Telephone contact' where x.person_id = a.patient_id limit 1) as phone_number
    , (select x.value_numeric from obs x where x.concept_id=167162 and x.person_id=a.patient_id and x.value_numeric>0 order by x.obs_datetime desc limit 1) as risk_classification
    , (select x.obs_datetime from obs x where x.concept_id=167162 and x.person_id=a.patient_id and x.value_numeric>0 order by x.obs_datetime desc limit 1) as risk_classification_date
    , 'Pending' as appointment_status
    , c.name
    , d.county_district as county
    , d.state_province as sub_county
    , d.address4 as ward
    , d.city_village
    , d.address2 as landmark
    , d.address5
    , d.address6
    , (select y.name from obs x inner join concept_name y on x.value_coded=y.concept_id where x.concept_id =1054 and y.locale ='en' and x.person_id=a.patient_id limit 1) as marital_status
    , null as case_manager_assigned
    , (select x.value_reference from location_attribute x where x.location_id = a.location_id limit 1) as facility_mfl
    , (select x.name from location x where x.location_id = a.location_id) as facility_name
    , (select case when x.value_coded=1065 then 'Yes' else 'No' end as response from obs x where x.concept_id=166607 and x.person_id=a.patient_id order by obs_datetime desc limit 1) as consented
    , (SELECT GROUP_CONCAT(y.name SEPARATOR ', ') FROM patient_program x INNER JOIN program y ON x.program_id = y.program_id and x.patient_id=a.patient_id WHERE x.date_completed IS NULL) AS program
    , (select risk_score from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id and x.description <> 'Unknown Risk' order by x.evaluation_date desc limit 1) as risk_score
    , (select description from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id and x.description <> 'Unknown Risk' order by x.evaluation_date desc limit 1) as risk_description
    , (select risk_factors from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id and x.description <> 'Unknown Risk' order by x.evaluation_date desc limit 1) as risk_factors
    , (select evaluation_date from kenyaemr_ml_patient_risk_score x where x.patient_id = a.patient_id and x.description <> 'Unknown Risk' order by x.evaluation_date desc limit 1) as risk_date
	, null as lmp
	, null as edd
	, null as delivery_date
	, null as intervention_list
	, null as cormobidities
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id limit 1) as enrollment_date
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id limit 1) as start_ART_date
	, (select x.date_enrolled from patient_program x inner join program y on x.program_id =y.program_id where y.name ='HIV' and x.patient_id =a.patient_id order by x.date_enrolled desc limit 1) as restart_ART_date
    from visit a
    inner join person b on a.patient_id = b.person_id
    left join visit_type c on a.visit_type_id = c.visit_type_id
    left join person_address d on a.patient_id = d.person_id
    where a.date_started >= %s and a.date_started <= %s
    and a.patient_id in (select x.patient_id from patient_program x
						inner join program y on x.program_id =y.program_id
						where y.name ='HIV')
) a where a.consented='Yes' group by a.patient_id, a.appointment_date
"""

# A KenyaEMR container carries the whole national facility list in `location`
# (~13,700 rows), so the container's own identity has to come from the
# kenyaemr.defaultLocation global property, not from scanning locations. The MFL
# is then read with the same expression APPOINTMENT_QUERY uses, so what the probe
# reports is exactly what an upload would send.
PROBE_DEFAULT_LOCATION_QUERY = """
SELECT l.name AS facility_name,
       (SELECT x.value_reference FROM location_attribute x
         WHERE x.location_id = l.location_id LIMIT 1) AS facility_mfl
FROM location l
WHERE l.location_id = (
    SELECT property_value FROM global_property
    WHERE property = 'kenyaemr.defaultLocation' AND property_value <> '' LIMIT 1
)
"""

# Used when kenyaemr.defaultLocation is unset. Reads the locations the most
# recent visits actually reference — bounded, because `visit` can be huge.
PROBE_VISIT_LOCATION_QUERY = """
SELECT (SELECT x.name FROM location x WHERE x.location_id = v.location_id) AS facility_name,
       (SELECT x.value_reference FROM location_attribute x
         WHERE x.location_id = v.location_id LIMIT 1) AS facility_mfl
FROM (
    SELECT DISTINCT location_id
    FROM (SELECT location_id FROM visit ORDER BY visit_id DESC LIMIT 1000) recent
) v
"""


@dataclass(frozen=True, repr=False)
class FacilityConfig:
    """Everything needed to reach one facility's OpenMRS MySQL."""

    label: str
    host: str
    port: int
    user: str
    password: str
    database: str
    pk: Optional[int] = None

    def __repr__(self):
        # Explicit, because this object ends up in tracebacks that are stored on
        # UploadLog.error_message and rendered in the browser.
        return (
            "FacilityConfig(label={!r}, host={!r}, port={!r}, "
            "database={!r}, user={!r}, password=***)".format(
                self.label, self.host, self.port, self.database, self.user,
            )
        )


def env_config():
    """The single-facility connection, built from OPENMRS_DB_* environment vars."""
    return FacilityConfig(
        label=settings.OPENMRS_DB_LABEL,
        host=settings.OPENMRS_DB_HOST,
        port=int(settings.OPENMRS_DB_PORT or 3306),
        user=settings.OPENMRS_DB_USER,
        password=settings.OPENMRS_DB_PASSWORD,
        database=settings.OPENMRS_DB_NAME,
    )


@contextmanager
def connect(config):
    """Open a MySQL connection to one facility, always closing it afterwards."""
    conn = MySQLdb.connect(
        host=config.host,
        port=int(config.port),
        user=config.user,
        password=config.password,
        database=config.database,
        charset='utf8mb4',
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
    )
    try:
        yield conn
    finally:
        conn.close()


def _safe_str(value, default=''):
    """Convert a value to string, returning default for None/empty."""
    if value is None:
        return default
    return str(value).strip() or default


def _safe_int(value, default=0):
    """Convert a value to int, returning default on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value, default=0.0):
    """Convert a value to float, returning default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def fetch_appointments(conn, date_from, date_to):
    """Query one facility's OpenMRS database for appointments in a date range."""
    cursor = conn.cursor()
    try:
        cursor.execute(APPOINTMENT_QUERY, [
            date_from.strftime('%Y-%m-%d'),
            date_to.strftime('%Y-%m-%d'),
        ])
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
    finally:
        cursor.close()

    patients = []
    for row in rows:
        record = dict(zip(columns, row))
        patients.append({
            'patient_id': _safe_str(record.get('patient_id')),
            'patient_name': _safe_str(record.get('patient_name')),
            'gender': _safe_str(record.get('gender')),
            'dob': _safe_str(record.get('dob')),
            'age': _safe_int(record.get('age')),
            'ccc_number': _safe_str(record.get('ccc_number')),
            'phone_number': _safe_str(record.get('phone_number')),
            'email': '',
            'physical_address': _safe_str(record.get('city_village')),
            'enrollment_date': _safe_str(record.get('enrollment_date')),
            'county': _safe_str(record.get('county')),
            'sub_county': _safe_str(record.get('sub_county')),
            'ward': _safe_str(record.get('ward')),
            'city_village': _safe_str(record.get('city_village')),
            'landmark': _safe_str(record.get('landmark')),
            'address5': _safe_str(record.get('address5')),
            'address6': _safe_str(record.get('address6')),
            'marital_status': _safe_str(record.get('marital_status')),
            'facility_mfl': _safe_str(record.get('facility_mfl')),
            'facility_name': _safe_str(record.get('facility_name')),
            'risk_classification': _safe_str(record.get('risk_classification'), 'Unknown'),
            'risk_classification_value': _safe_float(record.get('risk_classification_value')),
            'risk_classification_date': _safe_str(record.get('risk_classification_date')),
            'risk_factors': _safe_str(record.get('risk_factors')),
            'last_viral_load': _safe_str(record.get('last_viral_load')),
            'last_viral_load_date': _safe_str(record.get('last_viral_load_date')),
            'last_cd4': _safe_str(record.get('last_cd4')),
            'last_cd4_date': _safe_str(record.get('last_cd4_date')),
            'appointment_status': _safe_str(record.get('appointment_status'), 'Pending'),
            'appointment_date': _safe_str(record.get('appointment_date')),
            'visit_date': _safe_str(record.get('visit_date')),
            'case_manager_assigned': _safe_str(record.get('case_manager_assigned')),
            'program': _safe_str(record.get('program')),
            'lmp': _safe_str(record.get('lmp')),
            'edd': _safe_str(record.get('edd')),
            'delivery_date': _safe_str(record.get('delivery_date')),
            'intervention_list': _safe_str(record.get('intervention_list')),
            'cormobidities': _safe_str(record.get('cormobidities')),
            'start_ART_date': _safe_str(record.get('start_ART_date')),
            'restart_ART_date': _safe_str(record.get('restart_ART_date')),
        })
    return patients


def probe(config):
    """Connect to a facility and report what an upload would identify it as.

    The DIFF platform keys everything on the MFL code, so a facility whose MFL is
    blank, or whose MFL collides with another container, is misconfigured in a way
    that only shows up as bad data upstream. Surfacing it here means it gets
    caught during setup instead.
    """
    result = {
        'ok': False,
        'message': '',
        'mfl': '',
        'facility_name': '',
        'candidates': [],
        'mysql_version': '',
    }
    try:
        with connect(config) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT VERSION()')
                result['mysql_version'] = _safe_str(cursor.fetchone()[0])

                cursor.execute(PROBE_DEFAULT_LOCATION_QUERY)
                rows = cursor.fetchall()
                if not rows:
                    cursor.execute(PROBE_VISIT_LOCATION_QUERY)
                    rows = cursor.fetchall()
            finally:
                cursor.close()
    except Exception as exc:
        result['message'] = "Could not connect: {}".format(exc)
        return result

    result['candidates'] = [
        {'name': _safe_str(r[0]), 'mfl': _safe_str(r[1])} for r in rows
    ]

    if not result['candidates']:
        result['message'] = (
            'Connected, but this container has no default location set '
            '(kenyaemr.defaultLocation) and no visits to infer one from. It cannot '
            'be identified upstream.'
        )
        return result

    if len(result['candidates']) > 1:
        result['message'] = (
            'Connected, but {} locations were found and no default location is set. '
            'Set kenyaemr.defaultLocation in this container before '
            'uploading.'.format(len(result['candidates']))
        )
        return result

    only = result['candidates'][0]
    result['mfl'] = only['mfl']
    result['facility_name'] = only['name']
    if not only['mfl']:
        result['message'] = (
            'Connected to "{}", but it has no MFL code. Uploads from this container '
            'would be rejected.'.format(only['name'] or 'unnamed location')
        )
        return result

    result['ok'] = True
    result['message'] = 'Connected to {} (MFL {}) on MySQL {}.'.format(
        only['name'] or 'unnamed location', only['mfl'], result['mysql_version'],
    )
    return result
