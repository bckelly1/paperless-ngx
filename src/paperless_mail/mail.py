import os
import re
import tempfile
from datetime import date
from datetime import timedelta
from fnmatch import fnmatch
from imaplib import IMAP4

import magic
import pathvalidate
from django.conf import settings
from django.db import DatabaseError
from django_q.tasks import async_task
from documents.loggers import LoggingMixin
from documents.models import Correspondent
from documents.parsers import is_mime_type_supported
from imap_tools import AND
from imap_tools import MailBox
from imap_tools import MailboxFolderSelectError
from imap_tools import MailBoxUnencrypted
from imap_tools import MailMessage
from imap_tools import MailMessageFlags
from imap_tools import NOT
from imap_tools.mailbox import MailBoxTls
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule


class MailError(Exception):
    pass


class BaseMailAction:
    def get_criteria(self):
        return {}

    def post_consume(self, M, message_uids, parameter):
        pass  # pragma: nocover


class DeleteMailAction(BaseMailAction):
    def post_consume(self, M, message_uids, parameter):
        M.delete(message_uids)


class MarkReadMailAction(BaseMailAction):
    def get_criteria(self):
        return {"seen": False}

    def post_consume(self, M, message_uids, parameter):
        M.flag(message_uids, [MailMessageFlags.SEEN], True)


class MoveMailAction(BaseMailAction):
    def post_consume(self, M, message_uids, parameter):
        M.move(message_uids, parameter)


class FlagMailAction(BaseMailAction):
    def get_criteria(self):
        return {"flagged": False}

    def post_consume(self, M, message_uids, parameter):
        M.flag(message_uids, [MailMessageFlags.FLAGGED], True)


class TagMailAction(BaseMailAction):
    def __init__(self, parameter):
        self.keyword = parameter

    def get_criteria(self):
        return {"no_keyword": self.keyword, "gmail_label": self.keyword}

    def post_consume(self, M: MailBox, message_uids, parameter):
        if re.search(r"gmail\.com$|googlemail\.com$", M._host):
            for uid in message_uids:
                M.client.uid("STORE", uid, "X-GM-LABELS", self.keyword)
        else:
            M.flag(message_uids, [self.keyword], True)


def get_rule_action(rule):
    if rule.action == MailRule.MailAction.FLAG:
        return FlagMailAction()
    elif rule.action == MailRule.MailAction.DELETE:
        return DeleteMailAction()
    elif rule.action == MailRule.MailAction.MOVE:
        return MoveMailAction()
    elif rule.action == MailRule.MailAction.MARK_READ:
        return MarkReadMailAction()
    elif rule.action == MailRule.MailAction.TAG:
        return TagMailAction(rule.action_parameter)
    else:
        raise NotImplementedError("Unknown action.")  # pragma: nocover


def make_criterias(rule):
    maximum_age = date.today() - timedelta(days=rule.maximum_age)
    criterias = {}
    if rule.maximum_age > 0:
        criterias["date_gte"] = maximum_age
    if rule.filter_from:
        criterias["from_"] = rule.filter_from
    if rule.filter_subject:
        criterias["subject"] = rule.filter_subject
    if rule.filter_body:
        criterias["body"] = rule.filter_body

    return {**criterias, **get_rule_action(rule).get_criteria()}


def get_mailbox(server, port, security):
    if security == MailAccount.ImapSecurity.NONE:
        mailbox = MailBoxUnencrypted(server, port)
    elif security == MailAccount.ImapSecurity.STARTTLS:
        mailbox = MailBoxTls(server, port)
    elif security == MailAccount.ImapSecurity.SSL:
        mailbox = MailBox(server, port)
    else:
        raise NotImplementedError("Unknown IMAP security")  # pragma: nocover
    return mailbox


class MailAccountHandler(LoggingMixin):

    logging_name = "paperless_mail"

    def _correspondent_from_name(self, name):
        try:
            return Correspondent.objects.get_or_create(name=name)[0]
        except DatabaseError as e:
            self.log("error", f"Error while retrieving correspondent {name}: {e}")
            return None

    def get_title(self, message, att, rule):
        if rule.assign_title_from == MailRule.TitleSource.FROM_SUBJECT:
            return message.subject

        elif rule.assign_title_from == MailRule.TitleSource.FROM_FILENAME:
            return os.path.splitext(os.path.basename(att.filename))[0]

        else:
            raise NotImplementedError(
                "Unknown title selector.",
            )  # pragma: nocover

    def get_correspondent(self, message: MailMessage, rule):
        c_from = rule.assign_correspondent_from

        if c_from == MailRule.CorrespondentSource.FROM_NOTHING:
            return None

        elif c_from == MailRule.CorrespondentSource.FROM_EMAIL:
            return self._correspondent_from_name(message.from_)

        elif c_from == MailRule.CorrespondentSource.FROM_NAME:
            from_values = message.from_values
            if from_values is not None and len(from_values.name) > 0:
                return self._correspondent_from_name(from_values.name)
            else:
                return self._correspondent_from_name(message.from_)

        elif c_from == MailRule.CorrespondentSource.FROM_CUSTOM:
            return rule.assign_correspondent

        else:
            raise NotImplementedError(
                "Unknown correspondent selector",
            )  # pragma: nocover

    def handle_mail_account(self, account):

        self.renew_logging_group()

        self.log("debug", f"Processing mail account {account}")

        total_processed_files = 0
        try:
            with get_mailbox(
                account.imap_server,
                account.imap_port,
                account.imap_security,
            ) as M:

                try:
                    M.login(account.username, account.password)

                except UnicodeEncodeError:
                    self.log("debug", "Falling back to AUTH=PLAIN")
                    try:
                        # rfc2595 section 6 - PLAIN SASL mechanism
                        client: IMAP4 = M.client
                        encoded = (
                            b"\0"
                            + account.username.encode("utf8")
                            + b"\0"
                            + account.password.encode("utf8")
                        )
                        # Assumption is the server supports AUTH=PLAIN capability
                        # Could check the list with client.capability(), but then what?
                        # We're failing anyway then
                        client.authenticate("PLAIN", lambda x: encoded)

                        # Need to transition out of AUTH state to SELECTED
                        M.folder.set("INBOX")
                    except Exception as err:
                        self.log(
                            "error",
                            "Unable to authenticate with mail server using AUTH=PLAIN",
                        )
                        raise MailError(
                            f"Error while authenticating account {account}",
                        ) from err
                except Exception as e:
                    self.log(
                        "error",
                        f"Error while authenticating account {account}: {e}",
                        exc_info=False,
                    )
                    raise MailError(
                        f"Error while authenticating account {account}",
                    ) from e

                self.log(
                    "debug",
                    f"Account {account}: Processing "
                    f"{account.rules.count()} rule(s)",
                )

                for rule in account.rules.order_by("order"):
                    try:
                        total_processed_files += self.handle_mail_rule(M, rule)
                    except Exception as e:
                        self.log(
                            "error",
                            f"Rule {rule}: Error while processing rule: {e}",
                            exc_info=True,
                        )
        except MailError:
            raise
        except Exception as e:
            self.log(
                "error",
                f"Error while retrieving mailbox {account}: {e}",
                exc_info=False,
            )

        return total_processed_files

    def handle_mail_rule(self, M: MailBox, rule):

        self.log("debug", f"Rule {rule}: Selecting folder {rule.folder}")

        try:
            M.folder.set(rule.folder)
        except MailboxFolderSelectError as err:

            self.log(
                "error",
                f"Unable to access folder {rule.folder}, attempting folder listing",
            )
            try:
                for folder_info in M.folder.list():
                    self.log("info", f"Located folder: {folder_info.name}")
            except Exception as e:
                self.log(
                    "error",
                    "Exception during folder listing, unable to provide list folders: "
                    + str(e),
                )

            raise MailError(
                f"Rule {rule}: Folder {rule.folder} "
                f"does not exist in account {rule.account}",
            ) from err

        criterias = make_criterias(rule)
        criterias_imap = AND(**criterias)
        if "gmail_label" in criterias:
            gmail_label = criterias["gmail_label"]
            del criterias["gmail_label"]
            criterias_imap = AND(NOT(gmail_label=gmail_label), **criterias)

        self.log(
            "debug",
            f"Rule {rule}: Searching folder with criteria " f"{str(criterias_imap)}",
        )

        try:
            messages = M.fetch(
                criteria=criterias_imap,
                mark_seen=False,
                charset=rule.account.character_set,
            )
        except Exception as err:
            raise MailError(
                f"Rule {rule}: Error while fetching folder {rule.folder}",
            ) from err

        post_consume_messages = []

        mails_processed = 0
        total_processed_files = 0

        for message in messages:
            try:
                processed_files = self.handle_message(message, rule)
                if processed_files > 0:
                    post_consume_messages.append(message.uid)

                total_processed_files += processed_files
                mails_processed += 1
            except Exception as e:
                self.log(
                    "error",
                    f"Rule {rule}: Error while processing mail " f"{message.uid}: {e}",
                    exc_info=True,
                )

        self.log("debug", f"Rule {rule}: Processed {mails_processed} matching mail(s)")

        self.log(
            "debug",
            f"Rule {rule}: Running mail actions on "
            f"{len(post_consume_messages)} mails",
        )

        try:
            get_rule_action(rule).post_consume(
                M,
                post_consume_messages,
                rule.action_parameter,
            )

        except Exception as e:
            raise MailError(
                f"Rule {rule}: Error while processing post-consume actions: " f"{e}",
            ) from e

        return total_processed_files

    def handle_message(self, message, rule) -> int:
        if not message.attachments:
            return 0

        self.log(
            "debug",
            f"Rule {rule}: "
            f"Processing mail {message.subject} from {message.from_} with "
            f"{len(message.attachments)} attachment(s)",
        )

        correspondent = self.get_correspondent(message, rule)
        tag_ids = [tag.id for tag in rule.assign_tags.all()]
        doc_type = rule.assign_document_type

        processed_attachments = 0

        for att in message.attachments:

            if (
                not att.content_disposition == "attachment"
                and rule.attachment_type
                == MailRule.AttachmentProcessing.ATTACHMENTS_ONLY
            ):
                self.log(
                    "debug",
                    f"Rule {rule}: "
                    f"Skipping attachment {att.filename} "
                    f"with content disposition {att.content_disposition}",
                )
                continue

            if rule.filter_attachment_filename:
                # Force the filename and pattern to the lowercase
                # as this is system dependent otherwise
                if not fnmatch(
                    att.filename.lower(),
                    rule.filter_attachment_filename.lower(),
                ):
                    continue

            title = self.get_title(message, att, rule)

            # don't trust the content type of the attachment. Could be
            # generic application/octet-stream.
            mime_type = magic.from_buffer(att.payload, mime=True)

            if is_mime_type_supported(mime_type):

                os.makedirs(settings.SCRATCH_DIR, exist_ok=True)
                _, temp_filename = tempfile.mkstemp(
                    prefix="paperless-mail-",
                    dir=settings.SCRATCH_DIR,
                )
                with open(temp_filename, "wb") as f:
                    f.write(att.payload)

                self.log(
                    "info",
                    f"Rule {rule}: "
                    f"Consuming attachment {att.filename} from mail "
                    f"{message.subject} from {message.from_}",
                )

                async_task(
                    "documents.tasks.consume_file",
                    path=temp_filename,
                    override_filename=pathvalidate.sanitize_filename(
                        att.filename,
                    ),
                    override_title=title,
                    override_correspondent_id=correspondent.id
                    if correspondent
                    else None,
                    override_document_type_id=doc_type.id if doc_type else None,
                    override_tag_ids=tag_ids,
                    task_name=att.filename[:100],
                )

                processed_attachments += 1
            else:
                self.log(
                    "debug",
                    f"Rule {rule}: "
                    f"Skipping attachment {att.filename} "
                    f"since guessed mime type {mime_type} is not supported "
                    f"by paperless",
                )

        return processed_attachments
