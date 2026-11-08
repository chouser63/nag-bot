from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import time

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')}
scheduler = BackgroundScheduler(jobstores=jobstores)

def say_hi():
    print("hi from a persisted job")
    for job in scheduler.get_jobs():
        print(f"ID: {job.id}, Next run: {job.next_run_time}, Trigger: {job.trigger}")

scheduler.add_job(say_hi, 'interval', seconds=10, id='test_job', replace_existing=True)
scheduler.start()

while True:
    time.sleep(1)

## Need a get_jobs function, add it as a tool, modify the seed prompt to account for it
## Add a job at beginning of program just to test
## Ask bot to list jobs

## Create a create_job function, add as a tool, modify seed prompt for it
## Ask bot to add a job
## Ask bot to read jobs