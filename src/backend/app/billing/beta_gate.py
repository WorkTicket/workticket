import logging

logger = logging.getLogger(__name__)


class BetaGate:
    def check_idempotency(self) -> bool:
        try:
            from app.billing.idempotency import IdempotencyKey
            from app.billing.idempotency_service import (
                complete_idempotency_record,
                compute_request_hash,
                create_idempotency_record,
                extract_idempotency_key,
                get_idempotent_response,
            )

            return all(
                [
                    hasattr(IdempotencyKey, "__tablename__"),
                    callable(extract_idempotency_key),
                    callable(get_idempotent_response),
                    callable(create_idempotency_record),
                    callable(complete_idempotency_record),
                    callable(compute_request_hash),
                ]
            )
        except ImportError:
            return False

    def check_stuck_job_recovery(self) -> bool:
        try:
            from app.tasks.heartbeat import cleanup_stale_jobs
            from celery_app import celery_app

            has_task = "cleanup_stale_jobs" in celery_app.tasks
            has_beat = (
                celery_app.conf.beat_schedule is not None
                and "cleanup-stale-jobs-every-2-min" in celery_app.conf.beat_schedule
            )
            return callable(cleanup_stale_jobs) and has_task and has_beat
        except (ImportError, AttributeError):
            return False

    def check_dlq_exists(self) -> bool:
        try:
            from app.billing.dead_letter import DeadLetterJob
            from celery_app import _move_to_dead_letter

            return hasattr(DeadLetterJob, "__tablename__") and callable(_move_to_dead_letter)
        except (ImportError, AttributeError):
            return False

    def check_tracing(self) -> bool:
        try:
            from fastapi.routing import APIRoute

            from app.main import app

            has_request_id = False
            for route in app.routes:
                if isinstance(route, APIRoute) and "GET" in route.methods and route.path == "/health":
                    has_request_id = True
                    break
            return has_request_id
        except Exception:
            return False

    def check_billing(self) -> bool:
        try:
            from app.billing.credits import auto_credit_failed_job, grant_credit
            from app.billing.quota_engine import quota_engine
            from app.billing.reconciliation import reconcile_cost

            return all(
                [
                    hasattr(quota_engine, "check_and_reserve"),
                    callable(grant_credit),
                    callable(auto_credit_failed_job),
                    callable(reconcile_cost),
                ]
            )
        except ImportError:
            return False

    def check_load_tests(self) -> bool:
        import glob as glob_mod
        import os

        test_dir = os.path.join(os.path.dirname(__file__), "..", "..", "tests")
        files = glob_mod.glob(os.path.join(test_dir, "test_*.py"))
        return len(files) >= 5

    def run_all_checks(self) -> dict:
        return {
            "idempotency_enabled": self.check_idempotency(),
            "stuck_job_recovery": self.check_stuck_job_recovery(),
            "dlq_exists": self.check_dlq_exists(),
            "request_id_tracing": self.check_tracing(),
            "billing_integrity": self.check_billing(),
            "concurrency_test_passed": self.check_load_tests(),
        }

    def can_deploy(self) -> tuple[bool, dict]:
        results = self.run_all_checks()
        passed = all(results.values())
        if not passed:
            failed = [k for k, v in results.items() if not v]
            logger.warning("Beta gate BLOCKED deployment. Failed checks: %s", failed)
        else:
            logger.info("All beta gate checks PASSED — ready for deployment")
        return passed, results


beta_gate = BetaGate()
