#     Copyright 2014 Netflix, Inc.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
from datetime import datetime
import sys

from flask.ext.script import Manager, Command, Option, prompt_pass
from security_monkey.datastore import ExceptionLogs, clear_old_exceptions, store_exception

from security_monkey import app, db
from security_monkey.common.route53 import Route53Service

from flask.ext.migrate import Migrate, MigrateCommand

from security_monkey.scheduler import run_change_reporter as sm_run_change_reporter
from security_monkey.scheduler import find_changes as sm_find_changes
from security_monkey.scheduler import audit_changes as sm_audit_changes
from security_monkey.scheduler import disable_accounts as sm_disable_accounts
from security_monkey.scheduler import enable_accounts as sm_enable_accounts
from security_monkey.backup import backup_config_to_json as sm_backup_config_to_json
from security_monkey.common.utils import find_modules, load_plugins
from security_monkey.datastore import Account
from security_monkey.watcher import watcher_registry

try:
    from gunicorn.app.base import Application
    GUNICORN = True
except ImportError:
    # Gunicorn does not yet support Windows.
    # See issue #524. https://github.com/benoitc/gunicorn/issues/524
    # For dev on Windows, make this an optional import.
    print('Could not import gunicorn, skipping.')
    GUNICORN = False


manager = Manager(app)
migrate = Migrate(app, db)
manager.add_command('db', MigrateCommand)

find_modules('watchers')
find_modules('auditors')
load_plugins('security_monkey.plugins')

@manager.command
def drop_db():
    """ Drops the database. """
    db.drop_all()


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
def run_change_reporter(accounts):
    """ Runs Reporter """
    account_names = _parse_accounts(accounts)
    sm_run_change_reporter(account_names)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
def find_changes(accounts, monitors):
    """ Runs watchers """
    monitor_names = _parse_tech_names(monitors)
    account_names = _parse_accounts(accounts)
    sm_find_changes(account_names, monitor_names)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
@manager.option('-r', '--send_report', dest='send_report', type=bool, default=False)
def audit_changes(accounts, monitors, send_report):
    """ Runs auditors """
    monitor_names = _parse_tech_names(monitors)
    account_names = _parse_accounts(accounts)
    sm_audit_changes(account_names, monitor_names, send_report)


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
def delete_unjustified_issues(accounts, monitors):
    """ Allows us to delete unjustified issues. """
    monitor_names = _parse_tech_names(monitors)
    account_names = _parse_accounts(accounts)
    from security_monkey.datastore import ItemAudit
    # ItemAudit.query.filter_by(justified=False).delete()
    issues = ItemAudit.query.filter_by(justified=False).all()
    for issue in issues:
        del issue.sub_items[:]
        db.session.delete(issue)
    db.session.commit()


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
@manager.option('-m', '--monitors', dest='monitors', type=unicode, default=u'all')
@manager.option('-o', '--outputfolder', dest='outputfolder', type=unicode, default=u'backups')
def backup_config_to_json(accounts, monitors, outputfolder):
    """ Saves the most current item revisions to a json file. """
    monitor_names = _parse_tech_names(monitors)
    account_names = _parse_accounts(accounts)
    sm_backup_config_to_json(account_names, monitor_names, outputfolder)


@manager.command
def start_scheduler():
    """ Starts the python scheduler to run the watchers and auditors """
    from security_monkey import scheduler
    scheduler.setup_scheduler()
    scheduler.scheduler.start()


@manager.command
def sync_jira():
    """ Syncs issues with Jira """
    from security_monkey import jirasync
    if jirasync:
        app.logger.info('Syncing issues with Jira')
        jirasync.sync_issues()
    else:
        app.logger.info('Jira sync not configured. Is SECURITY_MONKEY_JIRA_SYNC set?')


@manager.command
def clear_expired_exceptions():
    """
    Clears out the exception logs table of all exception entries that have expired past the TTL.
    :return:
    """
    print("Clearing out exceptions that have an expired TTL...")
    clear_old_exceptions()
    print("Completed clearing out exceptions that have an expired TTL.")


@manager.command
def amazon_accounts():
    """ Pre-populates standard AWS owned accounts """
    import os
    import json
    from security_monkey.datastore import Account, AccountType

    data_file = os.path.join(os.path.dirname(__file__), "data", "aws_accounts.json")
    data = json.load(open(data_file, 'r'))

    app.logger.info('Adding / updating Amazon owned accounts')
    try:
        account_type_result = AccountType.query.filter(AccountType.name == 'AWS').first()
        if not account_type_result:
            account_type_result = AccountType(name='AWS')
            db.session.add(account_type_result)
            db.session.commit()
            db.session.refresh(account_type_result)

        for group, info in data.items():
            for aws_account in info['accounts']:
                acct_name = "{group} ({region})".format(group=group, region=aws_account['region'])
                account = Account.query.filter(Account.identifier == aws_account['account_id']).first()
                if not account:
                    app.logger.debug('    Adding account {0}'.format(acct_name))
                    account = Account()
                else:
                    app.logger.debug('    Updating account {0}'.format(acct_name))

                account.identifier = aws_account['account_id']
                account.account_type_id = account_type_result.id
                account.active = False
                account.third_party = True
                account.name = acct_name
                account.notes = info['url']

                db.session.add(account)

        db.session.commit()
        app.logger.info('Finished adding Amazon owned accounts')
    except Exception as e:
        app.logger.exception("An error occured while adding accounts")
        store_exception("manager-amazon-accounts", None, e)


@manager.option('-u', '--number', dest='number', type=unicode, required=True)
@manager.option('-a', '--active', dest='active', type=bool, default=True)
@manager.option('-t', '--thirdparty', dest='third_party', type=bool, default=False)
@manager.option('-n', '--name', dest='name', type=unicode, required=True)
@manager.option('-s', '--s3name', dest='s3_name', type=unicode, default=u'')
@manager.option('-o', '--notes', dest='notes', type=unicode, default=u'')
@manager.option('-y', '--type', dest='account_type', type=unicode, default=u'AWS')
@manager.option('-r', '--rolename', dest='role_name', type=unicode, default=u'SecurityMonkey')
@manager.option('-f', '--force', dest='force', help='Override existing accounts', action='store_true')
def add_account(number, third_party, name, s3_name, active, notes, account_type, role_name, force):
    from security_monkey.account_manager import account_registry
    account_manager = account_registry.get(account_type)()
    account = account_manager.lookup_account_by_identifier(number)
    if account:
        if force:
            account_manager.update(account.id, account_type, name, active,
                    third_party, notes, number,
                    custom_fields={ 's3_name': s3_name, 'role_name': role_name })
        else:
            app.logger.info('Account with id {} already exists'.format(number))
    else:
        account_manager.create(account_type, name, active, third_party, notes, number,
                    custom_fields={ 's3_name': s3_name, 'role_name': role_name })

    db.session.close()


@manager.command
@manager.option('-e', '--email', dest='email', type=unicode, required=True)
@manager.option('-r', '--role', dest='role', type=str, required=True)
def create_user(email, role):
    from flask_security import SQLAlchemyUserDatastore
    from security_monkey.datastore import User
    from security_monkey.datastore import Role
    user_datastore = SQLAlchemyUserDatastore(db, User, Role)

    ROLES = ['View', 'Comment', 'Justify', 'Admin']
    if role not in ROLES:
        sys.stderr.write('[!] Role must be one of [{0}].\n'.format(' '.join(ROLES)))
        sys.exit(1)

    users = User.query.filter(User.email == email)

    if users.count() == 0:
        password1 = prompt_pass("Password")
        password2 = prompt_pass("Confirm Password")

        if password1 != password2:
            sys.stderr.write("[!] Passwords do not match\n")
            sys.exit(1)

        user = user_datastore.create_user(email=email, password=password1, confirmed_at=datetime.now())
    else:
        sys.stdout.write("[+] Updating existing user\n")
        user = users.first()

    user.role = role

    db.session.add(user)
    db.session.commit()


@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
def disable_accounts(accounts):
    """ Bulk disables one or more accounts """
    account_names = _parse_accounts(accounts)
    sm_disable_accounts(account_names)

@manager.option('-a', '--accounts', dest='accounts', type=unicode, default=u'all')
def enable_accounts(accounts):
    """ Bulk enables one or more accounts """
    account_names = _parse_accounts(accounts, active=False)
    sm_enable_accounts(account_names)


def _parse_tech_names(tech_str):
    if tech_str == 'all':
        return watcher_registry.keys()
    else:
        return tech_str.split(',')


def _parse_accounts(account_str, active=True):
    if account_str == 'all':
        accounts = Account.query.filter(Account.third_party==False).filter(Account.active==active).all()
        accounts = [account.name for account in accounts]
        return accounts
    else:
        return account_str.split(',')


@manager.option('-n', '--name', dest='name', type=unicode, required=True)
def delete_account(name):
    from security_monkey.account_manager import delete_account_by_name
    delete_account_by_name(name)


@manager.option('-t', '--tech_name', dest='tech_name', type=str, required=True)
@manager.option('-d', '--disabled', dest='disabled', type=bool, default=False)
# We are locking down the allowed intervals here to 15 minutes, 1 hour, 12 hours, 24
# hours or one week because too many different intervals could result in too many
# scheduler threads, impacting performance.
@manager.option('-i', '--interval', dest='interval', type=int, default=1440, choices= [15, 60, 720, 1440, 10080])
def add_watcher_config(tech_name, disabled, interval):
    from security_monkey.datastore import WatcherConfig
    from security_monkey.watcher import watcher_registry

    if tech_name not in watcher_registry:
        sys.stderr.write('Invalid tech name {}.\n'.format(tech_name))
        sys.exit(1)

    query = WatcherConfig.query.filter(WatcherConfig.index == tech_name)
    entry = query.first()

    if not entry:
        entry = WatcherConfig()

    entry.index = tech_name
    entry.interval = interval
    entry.active = not disabled

    db.session.add(entry)
    db.session.commit()
    db.session.close()


class APIServer(Command):
    def __init__(self, host='127.0.0.1', port=app.config.get('API_PORT'), workers=6):
        self.address = "{}:{}".format(host, port)
        self.workers = workers

    def get_options(self):
        return (
            Option('-b', '--bind',
                   dest='address',
                   type=str,
                   default=self.address),
            Option('-w', '--workers',
                   dest='workers',
                   type=int,
                   default=self.workers),
        )

    def handle(self, app, *args, **kwargs):

        if app.config.get('USE_ROUTE53'):
            route53 = Route53Service()
            route53.register(app.config.get('FQDN'), exclusive=True)

        workers = kwargs['workers']
        address = kwargs['address']

        if not GUNICORN:
            print('GUNICORN not installed. Try `runserver` to use the Flask debug server instead.')
        else:
            class FlaskApplication(Application):
                def init(self, parser, opts, args):
                    return {
                        'bind': address,
                        'workers': workers
                    }

                def load(self):
                    return app

            FlaskApplication().run()


if __name__ == "__main__":
    manager.add_command("run_api_server", APIServer())
    manager.run()
