# -*- coding: utf-8 -*-
"""
This block defines a Staff Graded Assignment.  Students are shown a rubric
and invited to upload a file which is then graded by staff.
"""
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import pkg_resources
import pytz

from functools import partial

from courseware.models import StudentModule

from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.core.files.storage import default_storage
from django.conf import settings
from django.template import Context, Template
from django.utils.encoding import iri_to_uri

from student.models import user_by_anonymous_id
from submissions import api as submissions_api
from submissions.models import StudentItem as SubmissionsStudent

from webob.response import Response

from xblock.core import XBlock
from xblock.exceptions import JsonHandlerError
from xblock.fields import DateTime, Scope, String, Float, Integer, Boolean
from xblock.fragment import Fragment

from xmodule.util.duedate import get_extended_due_date


log = logging.getLogger(__name__)
BLOCK_SIZE = 8 * 1024


def reify(meth):
    """
    Decorator which caches value so it is only computed once.
    Keyword arguments:
    inst
    """
    def getter(inst):
        """
        Set value to meth name in dict and returns value.
        """
        value = meth(inst)
        inst.__dict__[meth.__name__] = value
        return value
    return property(getter)


class StaffGradedAssignmentXBlock(XBlock):
    """
    This block defines a Staff Graded Assignment.  Students are shown a rubric
    and invited to upload a file which is then graded by staff.
    """
    has_score = True
    icon_class = 'problem'
    STUDENT_FILEUPLOAD_MAX_SIZE = 4 * 1000 * 1000  # 4 MB

    display_name = String(
        display_name=u"Название задания",
        default=u'Задание, проверяемое преподавателем', scope=Scope.settings,
        help=u"Это название появляется в панели навигации вверху страницы."
    )

    weight = Float(
        display_name=u"Вес задания",
        help=(u"Задает количество баллов за один пункт задания. "
              u"Если значение не задано, баллы считаются как сумма "
              u"оценок по критериям."),
        values={"min": 0, "step": .1},
        scope=Scope.settings
    )

    points = Integer(
        display_name=u"Максимальная оценка",
        help=(u"Максимальная оценка за задание от преподавателя."),
        default=100,
        scope=Scope.settings
    )

    staff_score = Integer(
        display_name=u"Оценка не преподавателем",
        help=(u"Перед публикацией оценку должен будет подтвердить "
              u"преподаватель."),        
        default=None,
        scope=Scope.settings
    )

    comment = String(
        display_name=u"Комментарии преподавателя",
        default='',
        scope=Scope.user_state,
        help=u"Служат для обратной связи преподавателя со студентом."
    )

    annotated_sha1 = String(
        display_name=u"SHA1 файла с пометками",
        scope=Scope.user_state,
        default=None,
        help=(u"Контрольная сумма (по алгоритму sha1) файла с "
              u"пометками, загруженного преподавателем "
              u"по результатам выполнения задания.")
    )

    annotated_filename = String(
        display_name=u"Имя файла с пометками",
        scope=Scope.user_state,
        default=None,
        help=u"Имя файла с пометками, загруженного преподавателем."
    )

    annotated_mimetype = String(
        display_name=u"MIME-тип файла с пометками",
        scope=Scope.user_state,
        default=None,
        help=u"MIME-тип файла с пометками, загруженного преподавателем."
    )

    annotated_timestamp = DateTime(
        display_name=u"Дата и время",
        scope=Scope.user_state,
        default=None,
        help=u"Когда был загружен файл с пометками.")
    
    need_recheck = Boolean(
        display_name=u"Требуется ли перепроверка работы",
        scope=Scope.user_state,
        default=None,
        help=u"Работа была переписана, требуется перепроверка."
    )    

    def max_score(self):
        """
        Return the maximum score possible.
        """
        return self.points

    @reify
    def block_id(self):
        """
        Return the usage_id of the block.
        """
        return self.scope_ids.usage_id

    def student_submission_id(self, submission_id=None):
        # pylint: disable=no-member
        """
        Returns dict required by the submissions app for creating and
        retrieving submissions for a particular student.
        """
        if submission_id is None:
            submission_id = self.xmodule_runtime.anonymous_student_id
            assert submission_id != (
                'MOCK', "Forgot to call 'personalize' in test."
            )
        return {
            "student_id": submission_id,
            "course_id": self.course_id,
            "item_id": self.block_id,
            "item_type": 'sga',  # ???
        }

    def get_submission(self, submission_id=None):
        """
        Get student's most recent submission.
        """
        submissions = submissions_api.get_submissions(
            self.student_submission_id(submission_id))
        if submissions:
            # If I understand docs correctly, most recent submission should
            # be first
            return submissions[0]

    def get_score(self, submission_id=None):
        """
        Return student's current score.
        """
        score = submissions_api.get_score(
            self.student_submission_id(submission_id)
        )
        if score:
            return score['points_earned']

    @reify
    def score(self):
        """
        Return score from submissions.
        """
        return self.get_score()

    def student_view(self, context=None):
        # pylint: disable=no-member
        """
        The primary view of the StaffGradedAssignmentXBlock, shown to students
        when viewing courses.
        """
        context = {
            "student_state": json.dumps(self.student_state()),
            "id": self.location.name.replace('.', '_'),
            "max_file_size": getattr(
                settings, "STUDENT_FILEUPLOAD_MAX_SIZE",
                self.STUDENT_FILEUPLOAD_MAX_SIZE
            )
        }
        if self.show_staff_grading_interface():
            context['is_course_staff'] = True
            self.update_staff_debug_context(context)

        fragment = Fragment()
        fragment.add_content(
            render_template(
                'templates/staff_graded_assignment/show.html',
                context
            )
        )
        fragment.add_css(_resource("static/css/edx_sga.css"))
        fragment.add_javascript(_resource("static/js/src/edx_sga.js"))
        
        fragment.add_javascript_url("//cdn.datatables.net/1.10.16/js/jquery.dataTables.min.js")
        fragment.add_css_url("//cdn.datatables.net/1.10.16/css/jquery.dataTables.min.css")
        
        fragment.initialize_js('StaffGradedAssignmentXBlock', {'blockid': context['id']})
        return fragment

    def update_staff_debug_context(self, context):
        # pylint: disable=no-member
        """
        Add context info for the Staff Debug interface.
        """
        published = self.start
        context['is_released'] = published and published < _now()
        context['location'] = self.location
        context['category'] = type(self).__name__
        context['fields'] = [
            (name, field.read_from(self))
            for name, field in self.fields.items()]

    def student_state(self):
        """
        Returns a JSON serializable representation of student's state for
        rendering in client view.
        """
        submission = self.get_submission()
        if submission:
            uploaded = {"filename": submission['answer']['filename']}
        else:
            uploaded = None

        if self.annotated_sha1:
            annotated = {"filename": self.annotated_filename}
        else:
            annotated = None

        score = self.score
        if score is not None:
            graded = {'score': score, 'comment': self.comment}
        else:
            graded = None

        return {
            "display_name": self.display_name,
            "uploaded": uploaded,
            "annotated": annotated,
            "graded": graded,
            "max_score": self.max_score(),
            "upload_allowed": self.upload_allowed(),
            "need_recheck": self.need_recheck,
        }

    def staff_grading_data(self):
        """
        Return student assignment information for display on the
        grading screen.
        """
        def get_student_data():
            # pylint: disable=no-member
            """
            Returns a dict of student assignment information along with
            annotated file name, student id and module id, this
            information will be used on grading screen
            """
            # Submissions doesn't have API for this, just use model directly.
            students = SubmissionsStudent.objects.filter(
                course_id=self.course_id,
                item_id=self.block_id)
            for student in students:
                submission = self.get_submission(student.student_id)
                if not submission:
                    continue
                user = user_by_anonymous_id(student.student_id)
                module, created = StudentModule.objects.get_or_create(
                    course_id=self.course_id,
                    module_state_key=self.location,
                    student=user,
                    defaults={
                        'state': '{}',
                        'module_type': self.category,
                    })
                if created:
                    log.info(
                        "Init for course:%s module:%s student:%s  ",
                        module.course_id,
                        module.module_state_key,
                        module.student.username
                    )

                state = json.loads(module.state)
                score = self.get_score(student.student_id)
                approved = score is not None
                if score is None:
                    score = state.get('staff_score')
                    needs_approval = score is not None
                else:
                    needs_approval = False
                instructor = self.is_instructor()
                yield {
                    'module_id': module.id,
                    'student_id': student.student_id,
                    'submission_id': submission['uuid'],
                    'username': module.student.username,
                    'fullname': module.student.profile.name,
                    'filename': submission['answer']["filename"],
                    'timestamp': submission['created_at'].strftime(
                        #DateTime.DATETIME_FORMAT
                        #'%H:%M, %-d %B %Y'
                        '%Y-%m-%d %H:%M'
                    ),
                    'score': score,
                    'approved': approved,
                    #'needs_approval': instructor and needs_approval,
                    #'may_grade': instructor or not approved,
                    'needs_approval': needs_approval,
                    'may_grade': True,
                    'annotated': state.get("annotated_filename"),
                    'comment': state.get("comment", ''),
                    'need_recheck': state.get("need_recheck", False),
                }

        return {
            'assignments': list(get_student_data()),
            'max_score': self.max_score(),
            'display_name': self.display_name
        }

    def studio_view(self, context=None):
        """
        Return fragment for editing block in studio.
        """
        try:
            cls = type(self)

            def none_to_empty(data):
                """
                Return empty string if data is None else return data.
                """
                return data if data is not None else ''
            edit_fields = (
                (field, none_to_empty(getattr(self, field.name)), validator)
                for field, validator in (
                    (cls.display_name, 'string'),
                    (cls.points, 'number'),
                    (cls.weight, 'number'))
            )

            context = {
                'fields': edit_fields
            }
            fragment = Fragment()
            fragment.add_content(
                render_template(
                    'templates/staff_graded_assignment/edit.html',
                    context
                )
            )
            fragment.add_javascript(_resource("static/js/src/studio.js"))
            fragment.initialize_js('StaffGradedAssignmentXBlock')
            return fragment
        except:  # pragma: NO COVER
            log.error("Don't swallow my exceptions", exc_info=True)
            raise

    @XBlock.json_handler
    def save_sga(self, data, suffix=''):
        # pylint: disable=unused-argument
        """
        Persist block data when updating settings in studio.
        """
        self.display_name = data.get('display_name', self.display_name)

        # Validate points before saving
        points = data.get('points', self.points)
        # Check that we are an int
        try:
            points = int(points)
        except ValueError:
			raise JsonHandlerError(400, u'Баллы должны быть заданы в виде целого числа!')
        # Check that we are positive
        if points < 0:
			raise JsonHandlerError(400, u'Баллы должны быть заданы в виде целого положительного числа!')
        self.points = points

        # Validate weight before saving
        weight = data.get('weight', self.weight)

        # Mihara: This is a dirty hack, but I can't tell where exactly does the decimal comma come in this situation, so
        # doing that here.
        if type(weight) is str:
            weight = weight.replace(',','.')
        if type(weight) is unicode:
            weight = weight.replace(u',',u'.')

        # Check that weight is a float.
        if weight:
            try:
                weight = float(weight)
            except ValueError:
				raise JsonHandlerError(400, u'Вес должен быть задан в виде десятичного числа!')
            # Check that we are positive
            if weight < 0:
                raise JsonHandlerError(
                    400, u'Вес должен быть задан в виде положительного десятичного числа'
                )
        self.weight = weight

    @XBlock.handler
    def upload_assignment(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Save a students submission file.
        """
        require(self.upload_allowed())
        upload = request.params['assignment']
        sha1 = _get_sha1(upload.file)
        upload.file.name = upload.file.name.replace(',','_')
        answer = {
            "sha1": sha1,
            "filename": upload.file.name,
            "mimetype": mimetypes.guess_type(upload.file.name)[0],
        }
        student_id = self.student_submission_id()
        submissions_api.create_submission(student_id, answer)
        path = self._file_storage_path(sha1, upload.file.name)
        if not default_storage.exists(path):
            default_storage.save(path, File(upload.file))
            
        #if student already have score, set recheck to true
        if self.score is not None:
            self.need_recheck = True
        
        return Response(json_body=self.student_state())

    @XBlock.handler
    def staff_upload_annotated(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Save annotated assignment from staff.
        """
        require(self.is_course_staff())
        upload = request.params['annotated']
        upload.file.name = upload.file.name.replace(',','_')
        module = StudentModule.objects.get(pk=request.params['module_id'])
        state = json.loads(module.state)
        state['annotated_sha1'] = sha1 = _get_sha1(upload.file)
        state['annotated_filename'] = filename = upload.file.name
        state['annotated_mimetype'] = mimetypes.guess_type(upload.file.name)[0]
        state['annotated_timestamp'] = _now().strftime(
            DateTime.DATETIME_FORMAT
        )
        path = self._file_storage_path(sha1, filename)
        if not default_storage.exists(path):
            default_storage.save(path, File(upload.file))
        module.state = json.dumps(state)
        module.save()
        log.info(
            "staff_upload_annotated for course:%s module:%s student:%s ",
            module.course_id,
            module.module_state_key,
            module.student.username
        )
        return Response(json_body=self.staff_grading_data())

    @XBlock.handler
    def download_assignment(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Fetch student assignment from storage and return it.
        """
        answer = self.get_submission()['answer']
        path = self._file_storage_path(answer['sha1'], answer['filename'])
        return self.download(path, answer['mimetype'], answer['filename'])

    @XBlock.handler
    def download_annotated(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Fetch assignment with staff annotations from storage and return it.
        """
        path = self._file_storage_path(
            self.annotated_sha1,
            self.annotated_filename,
        )
        return self.download(
            path,
            self.annotated_mimetype,
            self.annotated_filename
        )

    @XBlock.handler
    def staff_download(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Return an assignment file requested by staff.
        """
        require(self.is_course_staff())
        submission = self.get_submission(request.params['student_id'])
        answer = submission['answer']
        path = self._file_storage_path(answer['sha1'], answer['filename'])
        return self.download(path, answer['mimetype'], answer['filename'])

    @XBlock.handler
    def staff_download_annotated(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Return annotated assignment file requested by staff.
        """
        require(self.is_course_staff())
        module = StudentModule.objects.get(pk=request.params['module_id'])
        state = json.loads(module.state)
        path = self._file_storage_path(
            state['annotated_sha1'],
            state['annotated_filename']
        )
        return self.download(
            path,
            state['annotated_mimetype'],
            state['annotated_filename']
        )

    def download(self, path, mime_type, filename):
        """
        Return a file from storage and return in a Response.
        """
        file_descriptor = default_storage.open(path)
        app_iter = iter(partial(file_descriptor.read, BLOCK_SIZE), '')
        return Response(
            app_iter=app_iter,
            content_type=mime_type,
            content_disposition="attachment; filename*=UTF-8''{0}".format(iri_to_uri(filename)))

    @XBlock.handler
    def get_staff_grading_data(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Return the html for the staff grading view
        """
        require(self.is_course_staff())
        return Response(json_body=self.staff_grading_data())

    @XBlock.handler
    def enter_grade(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Persist a score for a student given by staff.
        """
        
        #we need only numeric strings
        
        try:
            score = int(request.params['grade'])
        except:
            return Response(json_body=self.staff_grading_data())
        
        require(self.is_course_staff())
        module = StudentModule.objects.get(pk=request.params['module_id'])
        state = json.loads(module.state)
        #if self.is_instructor():
            #uuid = request.params['submission_id']
            #submissions_api.set_score(uuid, score, self.max_score())
        #else:
            #state['staff_score'] = score
        
        #approval all marks
        uuid = request.params['submission_id']
        submissions_api.set_score(uuid, score, self.max_score())
        
        state['need_recheck'] = False
        state['comment'] = request.params.get('comment', '')
        module.state = json.dumps(state)
        module.save()
        log.info(
            "enter_grade for course:%s module:%s student:%s",
            module.course_id,
            module.module_state_key,
            module.student.username
        )

        return Response(json_body=self.staff_grading_data())

    @XBlock.handler
    def remove_grade(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Reset a students score request by staff.
        """
        require(self.is_course_staff())
        student_id = request.params['student_id']
        submissions_api.reset_score(student_id, unicode(self.course_id), unicode(self.block_id))
        module = StudentModule.objects.get(pk=request.params['module_id'])
        state = json.loads(module.state)
        state['staff_score'] = None
        state['comment'] = ''
        state['annotated_sha1'] = None
        state['annotated_filename'] = None
        state['annotated_mimetype'] = None
        state['annotated_timestamp'] = None
        module.state = json.dumps(state)
        module.save()
        log.info(
            "remove_grade for course:%s module:%s student:%s",
            module.course_id,
            module.module_state_key,
            module.student.username
        )
        return Response(json_body=self.staff_grading_data())

    def is_course_staff(self):
        # pylint: disable=no-member
        """
         Check if user is course staff.
        """
        return getattr(self.xmodule_runtime, 'user_is_staff', False)

    def is_instructor(self):
        # pylint: disable=no-member
        """
        Check if user role is instructor.
        """
        return self.xmodule_runtime.get_user_role() == 'instructor'

    def show_staff_grading_interface(self):
        """
        Return if current user is staff and not in studio.
        """
        in_studio_preview = self.scope_ids.user_id is None
        return self.is_course_staff() and not in_studio_preview

    def past_due(self):
        """
        Return whether due date has passed.
        """
        due = get_extended_due_date(self)
        if due is not None:
            return _now() > due
        return False

    def upload_allowed(self):
        """
        Return whether student is allowed to submit an assignment.
        """
        #return not self.past_due() and self.score is None
        return not self.past_due()

    def _file_storage_path(self, sha1, filename):
        # pylint: disable=no-member
        """
        Get file path of storage.
        """
        path = (
            '{loc.org}/{loc.course}/{loc.block_type}/{loc.block_id}'
            '/{sha1}{ext}'.format(
                loc=self.location,
                sha1=sha1,
                ext=os.path.splitext(filename)[1]
            )
        )
        return path


def _get_sha1(file_descriptor):
    """
    Get file hex digest (fingerprint).
    """
    sha1 = hashlib.sha1()
    for block in iter(partial(file_descriptor.read, BLOCK_SIZE), ''):
        sha1.update(block)
    file_descriptor.seek(0)
    return sha1.hexdigest()


def _resource(path):  # pragma: NO COVER
    """
    Handy helper for getting resources from our kit.
    """
    data = pkg_resources.resource_string(__name__, path)
    return data.decode("utf8")


def _now():
    """
    Get current date and time.
    """
    return datetime.datetime.utcnow().replace(tzinfo=pytz.utc)


def load_resource(resource_path):  # pragma: NO COVER
    """
    Gets the content of a resource
    """
    resource_content = pkg_resources.resource_string(__name__, resource_path)
    return unicode(resource_content.decode('utf8'))


def render_template(template_path, context=None):  # pragma: NO COVER
    """
    Evaluate a template by resource path, applying the provided context.
    """
    if context is None:
        context = {}

    template_str = load_resource(template_path)
    template = Template(template_str)
    return template.render(Context(context))


def require(assertion):
    """
    Raises PermissionDenied if assertion is not true.
    """
    if not assertion:
        raise PermissionDenied
