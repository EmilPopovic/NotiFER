import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
from datetime import datetime
import threading

from shared import maintenance
from shared.crud import get_active_subscriptions_no_session
from shared.models import UserCalendar
from worker.services.calendar_service import CalendarService

logger = logging.getLogger(__name__)

class WorkerService:
    '''Service for managing the main worker loop.'''

    def __init__(self, calendar_service: CalendarService, worker_interval: int, max_workers: int = 3):
        self._terminate = threading.Event()
        self.calendar_service = calendar_service
        self.worker_interval = worker_interval
        self.max_workers = max_workers
        self._running: bool = False
        self.last_cycle: datetime | None = None

        # metrics
        self.worker_cycles_total = 0
        self.worker_cycle_duration = 0
        self.worker_last_cycle = 0
        self.subscriptions_processed: dict[str, int] = {}
        self.calendar_fetches = 0
        self.calendar_fetch_duration = 0
        self.emails_queued = 0

    def stop(self):
        """Signal the worker to stop processing"""
        self._terminate.set()

    def record_cycle_complete(self, start_time: float, status: str = 'success', subscription_count: int = 0):
        """Record completion of a processing cycle"""
        now = time.time()            
        self.worker_cycles_total += 1
        self.worker_cycle_duration = now - start_time
        self.worker_last_cycle = now
        
        logger.info(f'Cycle completed: status={status}, duration={self.worker_cycle_duration:.2f}s, subscriptions={subscription_count}')

    def record_subscription_processed(self, status: str):
        """Record processing of a subscription"""
        entry = self.subscriptions_processed.get(status, 0)
        self.subscriptions_processed[status] = entry + 1

    def record_calendar_fetch(self, status: str, duration: float):
        """Record calendar fetch attempt"""
        self.calendar_fetches += 1
        self.calendar_fetch_duration = duration

    def record_email_queued(self):
        """Record email being queued"""
        self.emails_queued += 1

    def process_subscription_with_metrics(self, subscription: UserCalendar) -> dict:
        '''Process single subscription with metrics tracking.'''
        start_time = time.time()

        try:
            result = self.calendar_service.process_subscription(subscription)
            duration = time.time() - start_time

            if result['error'] is None:
                self.record_subscription_processed('processed')
                self.record_calendar_fetch('success', duration)

                if result['email_queued']:
                    self.record_email_queued()
            else:
                self.record_subscription_processed('error')
                self.record_calendar_fetch('error', duration)

            return result
        
        except Exception as e:
            duration = time.time() - start_time
            self.record_subscription_processed('error')
            self.record_calendar_fetch('error', duration)
            logger.exception(f'Error processing subscription {subscription.email}: {e}')
            return {'error': 'UNDOCUMENTED_ERROR'}

    def process_subscription_batch(self, subscriptions: List[UserCalendar]) -> None:
        '''Process a batch of subscriptions using ThreadPoolExecutor.'''
        if not subscriptions:
            logger.info('No subscriptions to process')
            return
        
        logger.info(f'Processing {len(subscriptions)} subscriptions with {self.max_workers} workers')

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            futures = {
                executor.submit(self.process_subscription_with_metrics, sub): sub
                for sub in subscriptions
            }

            # Processs completed tasks
            successful = 0
            failed = 0
            for future in as_completed(futures):
                subscription = futures[future]
                try:
                    success = future.result()
                    if success['error'] is None:
                        successful += 1
                    else:
                        failed += 1

                except Exception as e:
                    logger.exception(f'Unhandled error processing subscription for {subscription.email}: {e}')
                    failed += 1

            logger.info(f'Batch processing complete: {successful} successful, {failed} failed')

    def run_single_cycle(self) -> bool:
        '''Run a single processing cycle'''
        logger.info('Starting processing cycle')
        cycle_start = time.time()
        maintenance.mark_cycle_start()

        try:
            # Get all subscriptions
            subscriptions = get_active_subscriptions_no_session()
            if not subscriptions:
                logger.info('No subscriptions found')
                self.record_cycle_complete(cycle_start, 'success', 0)
                return True
            
            logger.info(f'Found {len(subscriptions)} subscriptions')

            # Process subscriptions in parallel
            self.process_subscription_batch(subscriptions)

            self.record_cycle_complete(cycle_start, 'success', len(subscriptions))
            logger.info('Processing cycle complete')
            return True
        
        except Exception as e:
            logger.exception(f'Error in processing cycle: {e}')
            self.record_cycle_complete(cycle_start, 'error', 0)
            return False
        
        finally:
            maintenance.mark_cycle_end()
            self.last_cycle = datetime.now()

    def run_continuously(self) -> None:
        '''Run the worker in continuous mode.'''
        logger.info(f'Worker started with a {self.worker_interval}-second interval')

        while not self._terminate.is_set():
            try:
                cycle_start = time.time()

                # A data import holds the gate while it rewrites the subscription
                # table; skip the cycle rather than polling against moving rows.
                if maintenance.is_paused():
                    logger.info('Maintenance pause active, skipping this cycle')
                else:
                    self._running = True
                    self.run_single_cycle()
                    self._running = False

                # Calculate sleep time (ensure minimum interval)
                cycle_duration = time.time() - cycle_start
                sleep_time = max(0, self.worker_interval - cycle_duration)

                if sleep_time > 0:
                    logger.info(f'Cycle took {cycle_duration:.2f}s. Sleeping for {sleep_time:.2f}s')
                    # Interruptible so stop() takes effect without waiting out the interval.
                    self._terminate.wait(sleep_time)
                else:
                    logger.warning(f'Cycle took {cycle_duration:.2f}s, longer than interval {self.worker_interval}s')

            except KeyboardInterrupt:
                logger.info('Worker shutdown requested (KeyboardInterrupt). Exiting.')
                self._running = False
                break
            except Exception as e:
                logger.exception(f'Unexpected error in worker loop: {e}')
                self._running = False
                cycle_start_time = time.time()
                self.record_cycle_complete(cycle_start_time, 'critical_error', 0)
                logger.info(f'Sleeping for {self.worker_interval} seconds before retry')
                self._terminate.wait(self.worker_interval)

        logger.info('Worker loop exited')
