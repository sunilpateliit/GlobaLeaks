# -*- coding: UTF-8
#   submission
#   **********
#
#   Implements a GlobaLeaks submission, then the operations performed
#   by an HTTP client in /submission URI

from twisted.internet.defer import inlineCallbacks
from globaleaks.settings import transact
from globaleaks.models import *
from globaleaks import models
from globaleaks.handlers.base import BaseHandler
from globaleaks.jobs.notification_sched import APSNotification
from globaleaks.jobs.delivery_sched import APSDelivery
from globaleaks.runner import GLAsynchronous
from globaleaks.rest import requests
from globaleaks.utils import random_string, log, utcFutureDate
from globaleaks.rest.errors import *


def wb_serialize_internaltip(internaltip):
    response = {
        'id' : unicode(internaltip.id),
        'context_gus': unicode(internaltip.context_id),
        #'creation_date' : unicode(utils.prettyDateTime(internaltip.creation_date)),
        #'expiration_date' : unicode(utils.prettyDateTime(internaltip.creation_date)),
        'wb_fields' : dict(internaltip.wb_fields or {}),
        'download_limit' : int(internaltip.download_limit),
        'access_limit' : int(internaltip.access_limit),
        'mark' : unicode(internaltip.mark),
        'pertinence' : unicode(internaltip.pertinence_counter),
        'escalation_threshold' : unicode(internaltip.escalation_threshold),
        'files' : [],
        'receivers' : []
    }
    for receiver in internaltip.receivers:
        response['receivers'].append(receiver.id)

    for internalfile in internaltip.internalfiles:
        response['files'].append(internalfile.id)

    return response

@transact
def create_whistleblower_tip(store, submission):
    wbtip = WhistleblowerTip()
    wbtip.receipt = unicode(random_string(10, 'a-z,A-Z,0-9'))
    wbtip.access_counter = 0
    wbtip.internaltip_id = submission['id']
    store.add(wbtip)
    return wbtip.receipt


def import_receivers(store, submission, receiver_id_list, context, required=False):
    # As first we check if Context has some policies
    if not context.selectable_receiver:
        for receiver in context.receivers:
            submission.receivers.add(receiver)
        return

    # Clean the previous list of selected Receiver
    # store.remove(submission.receivers) -- DON'T WORK
    # for prevrec in submission.receivers:
    #    store.remove(prevrec) -- DON'T WORK
    #    submission.receivers.remove(prevrec) -- DON'T WORK EITHER
    # NEED TO BE DONE, DON'T KNOW HOW TO MAKE IT WORKS

    # import WB requests
    for receiver_id in receiver_id_list:
        try:
            receiver = store.find(Receiver, Receiver.id == unicode(receiver_id)).one()
        except Exception, e:
            log.err("Storm/SQL Error: %s" % e)
            raise e

        if not receiver:
            raise ReceiverGusNotFound
        submission.receivers.add(receiver)

    log.debug("Added %d Receivers as requested (%s)", submission.receivers.count(), str(receiver_id_list))

    if required and submission.receivers.count() == 0:
        raise SubmissionFailFields("Receivers required to be completed")


def import_files(store, submission, files):
    for file_id in files:
        try:
            file = store.find(InternalFile, InternalFile,id == unicode(file_id)).one()
        except Exception, e:
            log.err("Storm/SQL Error: %s" % e)
            raise e
        if not file:
            raise FileGusNotFound

        submission.internalfiles.add(file)

def import_fields(submission, fields, expected_fields, strict_validation=False):
    """
    @param submission: the Storm object
    @param fields: the received fields
    @param expected_fields: the Context defined fields
    @return: update the object of raise an Exception if a required field
        is missing, or if received field do not match the expected shape

    strict_validation = required the presence of 'required' fields. Is not enforced
    if Submission would not be finalized yet.
    """
    if strict_validation:
        if not fields:
            raise SubmissionFailFields("Missing submission!")

        for entry in expected_fields:
            if entry['required']:
                if not fields.has_key(entry['name']):
                    raise SubmissionFailFields("Missing field '%s': Required" % entry['name'])

    submission.wb_fields = {}
    if not fields:
        return

    for key, value in fields.iteritems():
        key_exists = False

        for entry in expected_fields:
            if key == entry['name']:
                key_exists = True
                break

        if not key_exists:
            raise SubmissionFailFields("Submitted field '%s' not expected in context" % key)

        try:
            submission.wb_fields.update({key: value})
        except Exception, e:
            log.err("Storm/SQL Error: %s" % e)
            raise e

    log.debug("Completed fields import (with key validation): %s", submission.wb_fields)


def force_schedule():
    # force mail sending, is called force_execution to be sure that Scheduler
    # run the Notification process, and not our callback+user event.
    # after two second create the Receiver tip, after five loop over the emails
    DeliverySched = APSDelivery()
    DeliverySched.force_execution(GLAsynchronous, seconds=2)
    NotifSched = APSNotification()
    NotifSched.force_execution(GLAsynchronous, seconds=5)


@transact
def create_submission(store, request, finalize):
    context = store.find(Context, Context.id == unicode(request['context_gus'])).one()
    del request['context_gus']

    if not context:
        raise ContextGusNotFound

    try:
        submission = InternalTip()
    except Exception, e:
        log.err("Storm/SQL Error: %s" % e)
        raise e


    submission.escalation_threshold = context.escalation_threshold
    submission.access_limit = context.tip_max_access
    submission.download_limit = context.file_max_download
    submission.expiration_date = utcFutureDate(hours=(context.tip_timetolive * 24))
    submission.pertinence_counter = 0
    submission.context_id = context.id
    submission.creation_date = models.now()

    if finalize:
        submission.mark = InternalTip._marker[0] # Submission
    else:
        submission.mark = InternalTip._marker[1] # Finalized

    receivers = request.get('receivers', [])
    import_receivers(store, submission, receivers, context, required=finalize)

    files = request.get('files', [])
    import_files(store, submission, files)

    fields = request.get('wb_fields', [])
    import_fields(submission, fields, context.fields, strict_validation=finalize)

    try:
        store.add(submission)
    except Exception, e:
        log.err("Storm/SQL Error: %s" % e)
        raise e

    submission_dict = wb_serialize_internaltip(submission)

    # API compatibility until stability is reached
    submission_dict['submission_gus'] = unicode(submission.id)
    return submission_dict

@transact
def update_submission(store, id, request, finalize):
    submission = store.find(InternalTip, InternalTip.id == unicode(id)).one()

    if not submission:
        raise SubmissionGusNotFound

    if submission.mark != InternalTip._marker[0]:
        raise SubmissionConcluded

    context = store.find(Context, Context.id == unicode(request['context_gus'])).one()
    if not context:
        raise ContextGusNotFound()

    # Can't be changed context in the middle of a Submission
    if submission.context_id != context.id:
        raise ContextGusNotFound()

    receivers = request.get('receivers', [])
    import_receivers(store, submission, receivers, context, required=finalize)

    files = request.get('files', [])
    import_files(store, submission, files)

    fields = request.get('wb_fields', [])
    import_fields(submission, fields, submission.context.fields, strict_validation=finalize)

    if finalize:
        submission.mark = InternalTip._marker[1] # Finalized

    submission_dict = wb_serialize_internaltip(submission)

    # API compatibility until stability is reached
    submission_dict['submission_gus'] = unicode(submission.id)
    return submission_dict


@transact
def get_submission(store, id):

    submission = store.find(InternalTip, InternalTip.id == unicode(id)).one()
    if not submission:
        raise SubmissionGusNotFound

    return wb_serialize_internaltip(submission)

@transact
def delete_submission(store, id):

    submission = store.find(InternalTip, InternalTip.id == unicode(id)).one()

    if not submission:
        raise SubmissionGusNotFound

    if submission.mark != submission._marked[0]:
        raise SubmissionConcluded

    store.delete(submission)


class SubmissionCreate(BaseHandler):
    """
    U2
    This class create the submission, receiving a partial wbSubmissionDesc, and
    returning a submission_gus, usable in update operation.
    """

    @inlineCallbacks
    def post(self, *uriargs):
        """
        Request: wbSubmissionDesc
        Response: wbSubmissionDesc
        Errors: ContextGusNotFound, InvalidInputFormat, SubmissionFailFields

        This creates an empty submission for the requested context,
        and returns submissionStatus with empty fields and a Submission Unique String,
        This is the unique token used during the submission procedure.
        sessionGUS is used as authentication secret for the next interaction.
        expire after the time set by Admin (Context dependent setting)
        """

        request = self.validate_message(self.request.body, requests.wbSubmissionDesc)

        if request['finalize']:
            finalize = True
        else:
            finalize = False

        status = yield create_submission(request, finalize)

        if finalize:
            receipt = yield create_whistleblower_tip(status)
            status.update({'receipt': receipt})
            force_schedule()
        else:
            status.update({'receipt' : ''})

        self.set_status(201) # Created
        self.finish(status)


class SubmissionInstance(BaseHandler):
    """
    U3
    This is the interface for create, populate and complete a submission.
    Relay in the client-server update and exchange of the submissionStatus message.
    """

    @inlineCallbacks
    def get(self, submission_gus, *uriargs):
        """
        Parameters: submission_gus
        Response: wbSubmissionDesc
        Errors: SubmissionGusNotFound, InvalidInputFormat

        Get the status of the current submission.
        """
        submission = yield get_submission(submission_gus)

        self.set_status(200)
        self.finish(submission)

    @inlineCallbacks
    def put(self, submission_gus, *uriargs):
        """
        Parameter: submission_gus
        Request: wbSubmissionDesc
        Response: wbSubmissionDesc
        Errors: ContextGusNotFound, InvalidInputFormat, SubmissionFailFields, SubmissionGusNotFound, SubmissionConcluded

        PUT update the submission and finalize if requested.
        """

        request = self.validate_message(self.request.body, requests.wbSubmissionDesc)

        if request['finalize']:
            finalize = True
        else:
            finalize = False

        status = yield update_submission(submission_gus, request)

        if finalize:
            receipt = yield create_whistleblower_tip(status)
            status.update({'receipt': receipt})
            force_schedule()
        else:
            status.update({'receipt' : ''})

        self.set_status(202) # Updated
        self.finish(status)


    @inlineCallbacks
    def delete(self, submission_gus, *uriargs):
        """
        Parameter: submission_gus
        Request:
        Response: None
        Errors: SubmissionGusNotFound, SubmissionConcluded

        A whistleblower is deleting a Submission because has understand that won't really be an hero. :P
        """

        yield delete_submission(submission_gus)

        self.set_status(200) # Accepted
        self.finish()


