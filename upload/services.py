import json
import logging
import traceback

import requests
from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

APPOINTMENT_QUERY = """
WITH vls AS (
    SELECT DISTINCT
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
    FROM openmrs.encounter e
    INNER JOIN openmrs.obs o
        ON e.encounter_id = o.encounter_id
        AND o.voided = 0
        AND o.concept_id IN (5497,730,654,790,856,1030,1305)
    LEFT JOIN openmrs.orders od
        ON od.order_id = o.order_id
        AND od.voided = 0
    LEFT JOIN openmrs.concept_name cn1
        ON cn1.concept_id = o.concept_id
        AND cn1.locale = 'en'
        AND cn1.concept_name_type = 'FULLY_SPECIFIED'
    LEFT JOIN openmrs.concept_name cn2
        ON cn2.concept_id = (
            CASE
                WHEN o.concept_id IN (1030,1305) THEN o.value_coded
            END
        )
        AND cn2.locale = 'en'
        AND cn2.concept_name_type = 'FULLY_SPECIFIED'
    WHERE e.voided = 0 AND cn1.name LIKE '%%viral%%'
)
SELECT
    MAX(a.patient_id) AS patient_id,
    MAX(a.visit_date) AS visit_date,
    MAX(appointment_date) AS appointment_date,
    MAX(patient_name) AS patient_name,
    MAX(a.gender) AS gender,
    MAX(a.dob) AS dob,
    MAX(a.age) AS age,
    MAX(a.ccc_number) AS ccc_number,
    MAX(a.phone_number) AS phone_number,
    a.risk_classification AS risk_classification_value,
    CASE
        WHEN MAX(a.risk_classification) IS NULL THEN 'Unknown'
        WHEN MAX(a.risk_classification) = 0 THEN 'Unknown'
        WHEN MAX(a.risk_classification) <= (
            SELECT property_value FROM global_property
            WHERE property='kenyaemrml.iit.lowRiskThreshold' LIMIT 1
        ) THEN 'Low Risk'
        WHEN MAX(a.risk_classification) <= (
            SELECT property_value FROM global_property
            WHERE property='kenyaemrml.iit.mediumRiskThreshold' LIMIT 1
        ) THEN 'Medium Risk'
        WHEN MAX(a.risk_classification) <= (
            SELECT property_value FROM global_property
            WHERE property='kenyaemrml.iit.highRiskThreshold' LIMIT 1
        ) THEN 'High Risk'
        ELSE 'Unknown'
    END AS risk_classification,
    MAX(a.risk_factors) AS risk_factors,
    (SELECT TestResults FROM vls x WHERE x.patient_id = a.patient_id
     ORDER BY VLDate DESC LIMIT 1) AS last_viral_load,
    MAX(a.appointment_status) AS appointment_status,
    MAX(a.county) AS county,
    MAX(a.sub_county) AS sub_county,
    MAX(a.ward) AS ward,
    MAX(a.city_village) AS city_village,
    MAX(a.landmark) AS landmark,
    MAX(a.address5) AS address5,
    MAX(a.address6) AS address6,
    MAX(a.marital_status) AS marital_status,
    MAX(a.case_manager_assigned) AS case_manager_assigned,
    MAX(a.facility_mfl) AS facility_mfl,
    MAX(a.facility_name) AS facility_name,
    MAX(a.consented) AS consented
FROM (
    SELECT DISTINCT
        a.patient_id,
        DATE(a.date_started) AS visit_date,
        (SELECT x.start_date_time FROM openmrs.patient_appointment x
         WHERE x.patient_id = a.patient_id AND x.status != 'Cancelled'
         AND x.voided = 0 ORDER BY x.start_date_time DESC LIMIT 1) AS appointment_date,
        (SELECT CONCAT(x.given_name, ' ', IFNULL(x.middle_name, ''), ' ', x.family_name)
         FROM openmrs.person_name x WHERE x.person_id = a.patient_id LIMIT 1) AS patient_name,
        b.gender,
        b.birthdate AS dob,
        ROUND(TIMESTAMPDIFF(MONTH, b.birthdate, NOW()) / 12.0, 0) AS age,
        (SELECT x.identifier FROM openmrs.patient_identifier x
         INNER JOIN openmrs.patient_identifier_type y
         ON x.identifier_type = y.patient_identifier_type_id
         AND y.name = 'Unique Patient Number'
         WHERE x.patient_id = a.patient_id LIMIT 1) AS ccc_number,
        (SELECT x.value FROM openmrs.person_attribute x
         INNER JOIN openmrs.person_attribute_type y
         ON x.person_attribute_type_id = y.person_attribute_type_id
         AND y.name = 'Telephone contact'
         WHERE x.person_id = a.patient_id LIMIT 1) AS phone_number,
        (SELECT x.value_numeric FROM obs x
         WHERE x.concept_id = 167162 AND x.person_id = a.patient_id
         ORDER BY x.obs_datetime DESC LIMIT 1) AS risk_classification,
        '' AS risk_factors,
        'Pending' AS appointment_status,
        c.name,
        d.county_district AS county,
        d.state_province AS sub_county,
        d.address4 AS ward,
        d.city_village,
        d.address2 AS landmark,
        d.address5,
        d.address6,
        NULL AS marital_status,
        NULL AS case_manager_assigned,
        (SELECT x.value_reference FROM openmrs.location_attribute x
         WHERE x.location_id = a.location_id) AS facility_mfl,
        (SELECT x.name FROM openmrs.location x
         WHERE x.location_id = a.location_id) AS facility_name,
        (SELECT CASE WHEN x.value_coded = 1065 THEN 'Yes' ELSE 'No' END AS response
         FROM obs x WHERE x.concept_id = 166607 AND x.person_id = a.patient_id
         ORDER BY obs_datetime DESC LIMIT 1) AS consented
    FROM openmrs.visit a
    INNER JOIN openmrs.person b ON a.patient_id = b.person_id
    LEFT JOIN openmrs.visit_type c ON a.visit_type_id = c.visit_type_id
    LEFT JOIN person_address d ON a.patient_id = d.person_id
    WHERE a.date_started >= %s AND a.date_started <= %s
    AND a.patient_id IN (
        SELECT x.patient_id FROM patient_program x
        INNER JOIN program y ON x.program_id = y.program_id
        WHERE y.name = 'HIV'
    )
) a WHERE a.consented = 'Yes'
GROUP BY a.patient_id, a.visit_date
"""


def fetch_appointments(date_from, date_to):
    """Query OpenMRS database for appointment data within a date range."""
    connection = connections['openmrs']
    with connection.cursor() as cursor:
        cursor.execute(APPOINTMENT_QUERY, [
            date_from.strftime('%Y-%m-%d'),
            date_to.strftime('%Y-%m-%d'),
        ])
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

    def safe_str(value, default=''):
        """Convert a value to string, returning default for None/empty."""
        if value is None:
            return default
        return str(value).strip() or default

    def safe_int(value, default=0):
        """Convert a value to int, returning default on failure."""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def safe_float(value, default=0.0):
        """Convert a value to float, returning default on failure."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    patients = []
    for row in rows:
        record = dict(zip(columns, row))
        patients.append({
            'patient_id': safe_str(record.get('patient_id')),
            'patient_name': safe_str(record.get('patient_name')),
            'gender': safe_str(record.get('gender')),
            'dob': safe_str(record.get('dob')),
            'age': safe_int(record.get('age')),
            'ccc_number': safe_str(record.get('ccc_number')),
            'phone_number': safe_str(record.get('phone_number')),
            'email': '',
            'physical_address': safe_str(record.get('city_village')),
            'enrollment_date': safe_str(record.get('visit_date')),
            'county': safe_str(record.get('county')),
            'sub_county': safe_str(record.get('sub_county')),
            'ward': safe_str(record.get('ward')),
            'city_village': safe_str(record.get('city_village')),
            'landmark': safe_str(record.get('landmark')),
            'address5': safe_str(record.get('address5')),
            'address6': safe_str(record.get('address6')),
            'marital_status': safe_str(record.get('marital_status')),
            'facility_mfl': safe_str(record.get('facility_mfl')),
            'facility_name': safe_str(record.get('facility_name')),
            'risk_classification': safe_str(record.get('risk_classification'), 'Unknown'),
            'risk_classification_value': safe_float(record.get('risk_classification_value')),
            'risk_factors': safe_str(record.get('risk_factors')),
            'last_viral_load': safe_str(record.get('last_viral_load')),
            'appointment_status': safe_str(record.get('appointment_status'), 'Pending'),
            'appointment_date': safe_str(record.get('appointment_date')),
            'visit_date': safe_str(record.get('visit_date')),
            'case_manager_assigned': safe_str(record.get('case_manager_assigned')),
        })
    return patients


def get_api_token():
    """Authenticate with the CHQI API and return a bearer token."""
    url = f"{settings.CHQI_API_BASE_URL}/api/auth/login"
    response = requests.post(
        url,
        data=json.dumps({
            'username': settings.CHQI_API_USERNAME,
            'password': settings.CHQI_API_PASSWORD,
        }),
        headers={'Content-Type': 'application/json'},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    logger.info("Login response keys: %s", list(data.keys()) if isinstance(data, dict) else data)
    # Try common token field names; search nested structures too
    token = (
        data.get('token')
        or data.get('access_token')
        or data.get('accessToken')
        or (data.get('data', {}) or {}).get('token')
        or (data.get('data', {}) or {}).get('access_token')
        or (data.get('data', {}) or {}).get('accessToken')
    )
    if not token:
        raise ValueError(f"Could not find token in login response: {data}")
    return token


def upload_patients(patients, token):
    """POST patient data to the CHQI API."""
    url = f"{settings.CHQI_API_BASE_URL}/api/patients/upload-json"
    response = requests.post(
        url,
        data=json.dumps({'patients': patients}),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        timeout=120,
    )
    if not response.ok:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise requests.HTTPError(
            f"HTTP {response.status_code}: {detail}",
            response=response,
        )
    return response.json()


def run_upload(date_from, date_to, triggered_by='manual', user=None):
    """Full pipeline: query OpenMRS, authenticate, upload, and log the result."""
    from .models import UploadLog

    log = UploadLog(
        date_from=date_from,
        date_to=date_to,
        triggered_by=triggered_by,
        triggered_by_user=user,
        status='failed',
    )

    try:
        patients = fetch_appointments(date_from, date_to)
        log.records_uploaded = len(patients)

        if not patients:
            log.status = 'success'
            log.error_message = 'No records found for the given period.'
            log.save()
            return log

        token = get_api_token()
        upload_patients(patients, token)

        log.status = 'success'
        log.save()
        logger.info(
            "Upload successful: %d records for %s to %s",
            len(patients), date_from, date_to,
        )
    except Exception:
        log.error_message = traceback.format_exc()
        log.save()
        logger.error(
            "Upload failed for %s to %s: %s",
            date_from, date_to, log.error_message,
        )

    return log
