# Copyright (c) 2011 Jyrki Pulliainen <jyrki@dywypi.org>
# Copyright (c) 2010 Inoi Oy
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use, copy,
# modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Asynchronous CouchDB client"""

import functools
import logging
import re
import collections
import tornado.ioloop

try:
    # Python 3
    from urllib.parse import quote as urlquote
    from urllib.parse import urlencode
except ImportError:
    # Python 2
    from urllib import quote as urlquote
    from urllib import urlencode

from base64 import b64encode, b64decode
from tornado.httpclient import AsyncHTTPClient
from tornado.httputil import HTTPHeaders

log = logging.getLogger('trombi')

try:
    import json
except ImportError:
    import simplejson as json

import trombi.errors


def from_uri(uri, fetch_args=None, io_loop=None, **kwargs):
    try:
        # Python 3
        from urllib.parse import urlparse, urlunsplit
    except ImportError:
        # Python 2
        from urlparse import urlparse, urlunsplit

    p = urlparse(uri)
    if p.params or p.query or p.fragment:
        raise ValueError(
            'Invalid database address: %s (extra query params)' % uri)
    if p.scheme != 'http':
        raise ValueError(
            'Invalid database address: %s (only http:// is supported)' % uri)

    baseurl = urlunsplit((p.scheme, p.netloc, '', '', ''))
    server = Server(baseurl, fetch_args, io_loop=io_loop, **kwargs)

    db_name = p.path.lstrip('/').rstrip('/')
    return Database(server, db_name)


class TrombiError(object):
    """
    A common error class denoting an error that has happened
    """
    error = True


class TrombiErrorResponse(TrombiError):
    def __init__(self, errno, msg):
        self.error = True
        self.errno = errno
        self.msg = msg

    def __str__(self):
        return 'CouchDB reported an error: %s (%d)' % (self.msg, self.errno)


class TrombiObject(object):
    """
    Dummy result for queries that really don't have anything sane to
    return, like succesful database deletion.

    """
    error = False


class TrombiResult(TrombiObject):
    """
    A generic result objects for Trombi queries that do not have any
    formal representation.
    """

    def __init__(self, data):
        self.content = data
        super(TrombiResult, self).__init__()


class TrombiDict(TrombiObject, dict):
    def to_basetype(self):
        return dict(self)


def _jsonize_params(params):
    result = dict()
    for key, value in params.items():
        result[key] = json.dumps(value)
    return urlencode(result)


def _error_response(response):
    if response.code == 599:
        return TrombiErrorResponse(599, 'Unable to connect to CouchDB')

    try:
        content = json.loads(response.body.decode('utf-8'))
    except ValueError:
        return TrombiErrorResponse(response.code, response.body)
    try:
        return TrombiErrorResponse(response.code, content['reason'])
    except (KeyError, TypeError):
        # TypeError is risen if the result is a list
        return TrombiErrorResponse(response.code, content)


class Server(TrombiObject):
    def __init__(self, baseurl, fetch_args=None, io_loop=None,
                 json_encoder=None, **client_args):
        self.error = False
        self.baseurl = baseurl
        if self.baseurl[-1] == '/':
            self.baseurl = self.baseurl[:-1]
        if fetch_args is None:
            self._fetch_args = dict()
        else:
            self._fetch_args = fetch_args

        if io_loop is None:
            self.io_loop = tornado.ioloop.IOLoop.instance()
        else:
            self.io_loop = io_loop
        # We can assign None to _json_encoder as the json (or
        # simplejson) then defaults to json.JSONEncoder
        self._json_encoder = json_encoder
        self._client = AsyncHTTPClient(self.io_loop, **client_args)

    def _invalid_db_name(self, name):
        return TrombiErrorResponse(
            trombi.errors.INVALID_DATABASE_NAME,
            'Invalid database name: %r' % name,
            )

    def _fetch(self, *args, **kwargs):
        # This is just a convenince wrapper for _client.fetch

        # Set default arguments for a fetch
        fetch_args = {
            'headers': HTTPHeaders({'Content-Type': 'application/json'})
        }
        fetch_args.update(self._fetch_args)
        fetch_args.update(kwargs)
        self._client.fetch(*args, **fetch_args)

    def create(self, name, callback):
        if not VALID_DB_NAME.match(name):
            # Avoid additional HTTP Query by doing the check here
            callback(self._invalid_db_name(name))

        def _create_callback(response):
            if response.code == 201:
                callback(Database(self, name))
            elif response.code == 412:
                callback(
                    TrombiErrorResponse(
                        trombi.errors.PRECONDITION_FAILED,
                        'Database already exists: %r' % name
                        ))
            else:
                callback(_error_response(response))

        self._fetch(
            '%s/%s' % (self.baseurl, name),
            _create_callback,
            method='PUT',
            body='',
            )

    def get(self, name, callback, create=False):
        if not VALID_DB_NAME.match(name):
            callback(self._invalid_db_name(name))

        def _really_callback(response):
            if response.code == 200:
                callback(Database(self, name))
            elif response.code == 404:
                # Database doesn't exist
                if create:
                    self.create(name, callback)
                else:
                    callback(TrombiErrorResponse(
                            trombi.errors.NOT_FOUND,
                            'Database not found: %s' % name
                            ))
            else:
                callback(_error_response(response))

        self._fetch(
            '%s/%s' % (self.baseurl, name),
            _really_callback,
            )

    def delete(self, name, callback):
        def _really_callback(response):
            if response.code == 200:
                callback(TrombiObject())
            elif response.code == 404:
                callback(
                    TrombiErrorResponse(
                        trombi.errors.NOT_FOUND,
                        'Database does not exist: %r' % name
                        ))
            else:
                callback(_error_response(response))

        self._fetch(
            '%s/%s' % (self.baseurl, name),
            _really_callback,
            method='DELETE',
            )

    def list(self, callback):
        def _really_callback(response):
            if response.code == 200:
                body = response.body.decode('utf-8')
                callback(Database(self, x) for x in json.loads(body))
            else:
                callback(_error_response(response))

        self._fetch(
            '%s/%s' % (self.baseurl, '_all_dbs'),
            _really_callback,
            )


class Database(TrombiObject):
    def __init__(self, server, name):
        self.server = server
        self._json_encoder = self.server._json_encoder
        self.name = name
        self.baseurl = '%s/%s' % (self.server.baseurl, self.name)

    def _fetch(self, url, *args, **kwargs):
        # Just a convenience wrapper
        if 'baseurl' in kwargs:
            url = '%s/%s' % (kwargs.pop('baseurl'), url)
        else:
            url = '%s/%s' % (self.baseurl, url)
        return self.server._fetch(url, *args, **kwargs)

    def info(self, callback):
        def _really_callback(response):
            if response.code == 200:
                body = response.body.decode('utf-8')
                callback(TrombiDict(json.loads(body)))
            else:
                callback(_error_response(response))

        self._fetch('', _really_callback)

    def set(self, *args, **kwargs):
        if len(args) == 2:
            data, callback = args
            doc_id = None
        elif len(args) == 3:
            doc_id, data, callback = args
        else:
            raise TypeError(
                'Database.set expected 2 or 3 arguments, got %d' % len(args))

        if kwargs:
            if list(kwargs.keys()) != ['attachments']:
                if len(kwargs) > 1:
                    raise TypeError(
                        '%s are invalid keyword arguments for this function') %(
                        (', '.join(kwargs.keys())))
                else:
                    raise TypeError(
                        '%s is invalid keyword argument for this function' % (
                            list(kwargs.keys())[0]))

            attachments = kwargs['attachments']
        else:
            attachments = {}

        if isinstance(data, Document):
            doc = data
        else:
            doc = Document(self, data)

        if doc_id is None and doc.id is not None and doc.rev is not None:
            # Update the existing document
            doc_id = doc.id

        if doc_id is not None:
            url = urlquote(doc_id, safe='')
            method = 'PUT'
        else:
            url = ''
            method = 'POST'

        for name, attachment in attachments.items():
            content_type, attachment_data = attachment
            if content_type is None:
                content_type = 'text/plain'
            doc.attachments[name] = {
                'content_type': content_type,
                'data': b64encode(attachment_data).decode('utf-8'),
                }

        def _really_callback(response):
            try:
                # If the connection to the server is malfunctioning,
                # ie. the simplehttpclient returns 599 and no body,
                # don't set the content as the response.code will not
                # be 201 at that point either
                if response.body is not None:
                    content = json.loads(response.body.decode('utf-8'))
            except ValueError:
                content = response.body

            if response.code == 201:
                doc.id = content['id']
                doc.rev = content['rev']
                callback(doc)
            else:
                callback(_error_response(response))

        self._fetch(
            url,
            _really_callback,
            method=method,
            body=json.dumps(doc.raw(), cls=self._json_encoder),
        )

    def get(self, doc_id, callback, attachments=False):
        def _really_callback(response):
            if response.code == 200:
                data = json.loads(response.body.decode('utf-8'))
                doc = Document(self, data)
                callback(doc)
            elif response.code == 404:
                # Document doesn't exist
                callback(None)
            else:
                callback(_error_response(response))

        doc_id = urlquote(doc_id, safe='')

        kwargs = {}

        if attachments is True:
            doc_id += '?attachments=true'
            kwargs['headers'] = HTTPHeaders(
                {'Content-Type': 'application/json',
                 'Accept': 'application/json',
             })

        self._fetch(
            doc_id,
            _really_callback,
            **kwargs
            )

    def get_attachment(self, doc_id, attachment_name, callback):
        def _really_callback(response):
            if response.code == 200:
                callback(response.body)
            elif response.code == 404:
                # Document or attachment doesn't exist
                callback(None)
            else:
                callback(_error_response(response))

        doc_id = urlquote(doc_id, safe='')
        attachment_name = urlquote(attachment_name, safe='')

        self._fetch(
            '%s/%s' % (doc_id, attachment_name),
            _really_callback,
            )

    def view(self, design_doc, viewname, callback, **kwargs):
        def _really_callback(response):
            if response.code == 200:
                body = response.body.decode('utf-8')
                callback(
                    ViewResult(json.loads(body), db=self)
                    )
            else:
                callback(_error_response(response))

        if not design_doc and viewname == '_all_docs':
            url = '_all_docs'
        else:
            url = '_design/%s/_view/%s' % (design_doc, viewname)

        # We need to pop keys before constructing the url to avoid it
        # ending up twice in the request, both in the body and as a
        # query parameter.
        keys = kwargs.pop('keys', None)

        if kwargs:
            url = '%s?%s' % (url, _jsonize_params(kwargs))

        if keys is not None:
            self._fetch(url, _really_callback,
                        method='POST',
                        body=json.dumps({'keys': keys})
                        )
        else:
            self._fetch(url, _really_callback)

    def list(self, design_doc, listname, viewname, callback, **kwargs):
        def _really_callback(response):
            if response.code == 200:
                callback(TrombiResult(response.body))
            else:
                callback(_error_response(response))

        url = '_design/%s/_list/%s/%s/' % (design_doc, listname, viewname)
        if kwargs:
            url = '%s?%s' % (url, _jsonize_params(kwargs))

        self._fetch(url, _really_callback)

    def temporary_view(self, callback, map_fun, reduce_fun=None,
                       language='javascript', **kwargs):
        def _really_callback(response):
            if response.code == 200:
                body = response.body.decode('utf-8')
                callback(
                    ViewResult(json.loads(body), db=self)
                    )
            else:
                callback(_error_response(response))

        url = '_temp_view'
        if kwargs:
            url = '%s?%s' % (url, _jsonize_params(kwargs))

        body = {'map': map_fun, 'language': language}
        if reduce_fun:
            body['reduce'] = reduce_fun

        self._fetch(url, _really_callback, method='POST',
                    body=json.dumps(body),
                    headers={'Content-Type': 'application/json'})

    def delete(self, data, callback):
        def _really_callback(response):
            try:
                json.loads(response.body.decode('utf-8'))
            except ValueError:
                callback(_error_response(response))
                return
            if response.code == 200:
                callback(self)
            else:
                callback(_error_response(response))

        if isinstance(data, Document):
            doc = data
        else:
            doc = Document(self, data)

        doc_id = urlquote(doc.id, safe='')
        self._fetch(
            '%s?rev=%s' % (doc_id, doc.rev),
            _really_callback,
            method='DELETE',
            )

    def bulk_docs(self, data, callback, all_or_nothing=False):
        def _really_callback(response):
            if response.code == 200 or response.code == 201:
                try:
                    content = json.loads(response.body.decode('utf-8'))
                except ValueError:
                    callback(TrombiErrorResponse(response.code, response.body))
                else:
                    callback(BulkResult(content))
            else:
                callback(_error_response(response))

        docs = []
        for element in data:
            if isinstance(element, Document):
                docs.append(element.raw())
            else:
                docs.append(element)

        payload = {'docs': docs}
        if all_or_nothing is True:
            payload['all_or_nothing'] = True

        self._fetch(
            '_bulk_docs',
            _really_callback,
            method='POST',
            body=json.dumps(payload),
            )

    def changes(self, callback, timeout=None, feed='normal', **kw):
        def _really_callback(response):
            log.debug('Changes feed response: %s', response)
            if response.code != 200:
                callback(_error_response(response))
                return
            if feed == 'continuous':
                # Feed terminated, call callback with None to indicate
                # this, if the mode is continous
                callback(None)
            else:
                body = response.body.decode('utf-8')
                callback(TrombiResult(json.loads(body)))

        stream_buffer = []

        def _stream(text):
            stream_buffer.append(text.decode('utf-8'))
            chunks = ''.join(stream_buffer).split('\n')

            # The last chunk is either an empty string or an
            # incomplete line. Save it for the next round. The [:]
            # syntax is used because of variable scoping.
            stream_buffer[:] = [chunks.pop()]

            for chunk in chunks:
                if not chunk.strip():
                    continue

                try:
                    obj = json.loads(chunk)
                except ValueError:
                    # JSON parsing failed. Apparently we have some
                    # gibberish on our hands, just discard it.
                    log.warning('Invalid changes feed line: %s' % chunk)
                    continue

                # "Escape" the streaming_callback context by invoking
                # the handler as an ioloop callback. This makes it
                # possible to start new HTTP requests in the handler
                # (it is impossible in the streaming_callback
                # context). Tornado runs these callbacks in the order
                # they were added, so this works correctly.
                #
                # This also relieves us from handling exceptions in
                # the handler.
                cb = functools.partial(callback, TrombiDict(obj))
                self.server.io_loop.add_callback(cb)

        couchdb_params = kw
        couchdb_params['feed'] = feed
        if timeout is not None:
            # CouchDB takes timeouts in milliseconds
            couchdb_params['timeout'] = timeout * 1000
        url = '_changes?%s' % urlencode(couchdb_params)
        params = dict()
        if feed == 'continuous':
            params['streaming_callback'] = _stream

        log.debug('Fetching changes from %s with params %s', url, params)
        self._fetch(url, _really_callback, **params)


class Document(collections.MutableMapping, TrombiObject):
    def __init__(self, db, data):
        self.db = db
        self.data = {}
        self.id = None
        self.rev = None
        self._postponed_attachments = False
        self.attachments = {}

        for key, value in data.items():
            if key.startswith('_'):
                setattr(self, key[1:], value)
            else:
                self[key] = value

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        return iter(self.data)

    def __contains__(self, key):
        return key in self.data

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        if key.startswith('_'):
            raise KeyError("Keys starting with '_' are reserved for CouchDB")
        self.data[key] = value

    def __delitem__(self, key):
        del self.data[key]

    def raw(self):
        result = {}
        if self.id:
            result['_id'] = self.id
        if self.rev:
            result['_rev'] = self.rev
        if self.attachments:
            result['_attachments'] = self.attachments

        result.update(self.data)
        return result

    def copy(self, new_id, callback):
        assert self.rev and self.id

        def _copy_done(response):
            if response.code != 201:
                callback(_error_response(response))
                return

            content = json.loads(response.body.decode('utf-8'))
            doc = Document(self.db, self.data)
            doc.attachments = self.attachments.copy()
            doc.id = content['id']
            doc.rev = content['rev']
            callback(doc)

        self.db._fetch(
            '%s' % urlquote(self.id, safe=''),
            _copy_done,
            allow_nonstandard_methods=True,
            method='COPY',
            headers={'Destination': str(new_id)}
            )

    def attach(self, name, data, callback, type='text/plain'):
        def _really_callback(response):
            if  response.code != 201:
                callback(_error_response(response))
                return
            data = json.loads(response.body.decode('utf-8'))
            assert data['id'] == self.id
            self.rev = data['rev']
            self.attachments[name] = {
                'content_type': type,
                'length': len(data),
                'stub': True,
            }
            callback(self)

        headers = {'Content-Type': type, 'Expect': ''}

        self.db._fetch(
            '%s/%s?rev=%s' % (
                urlquote(self.id, safe=''),
                urlquote(name, safe=''),
                self.rev),
            _really_callback,
            method='PUT',
            body=data,
            headers=headers,
            )

    def load_attachment(self, name, callback):
        def _really_callback(response):
            if response.code == 200:
                callback(response.body)
            else:
                callback(_error_response(response))

        if (hasattr(self, 'attachments') and
            name in self.attachments and
            not self.attachments[name].get('stub', False)):
            data = self.attachments[name]['data'].encode('utf-8')
            callback(b64decode(data))
        else:
            self.db._fetch(
                '%s/%s' % (
                    urlquote(self.id, safe=''),
                    urlquote(name, safe='')
                    ),
                _really_callback,
                )

    def delete_attachment(self, name, callback):
        def _really_callback(response):
            if response.code != 200:
                callback(_error_response(response))
                return
            callback(self)

        self.db._fetch(
            '%s/%s?rev=%s' % (self.id, name, self.rev),
            _really_callback,
            method='DELETE',
            )


class BulkError(TrombiError):
    def __init__(self, data):
        self.error_type = data['error']
        self.reason = data.get('reason', None)
        self.raw = data


class BulkObject(TrombiObject, collections.Mapping):
    def __init__(self, data):
        self._data = data

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def __contains__(self, key):
        return key in self._data

    def __getitem__(self, key):
        return self._data[key]


class BulkResult(TrombiResult, collections.Sequence):
    def __init__(self, result):
        self.content = []
        for line in result:
            if 'error' in line:
                self.content.append(BulkError(line))
            else:
                self.content.append(BulkObject(line))

    def __len__(self):
        return len(self.content)

    def __iter__(self):
        return iter(self.content)

    def __getitem__(self, key):
        return self.content[key]


class ViewResult(TrombiObject, collections.Sequence):
    def __init__(self, result, db=None):
        self.db = db
        self.total_rows = result.get('total_rows', len(result['rows']))
        self._rows = result['rows']
        self.offset = result.get('offset', 0)

    def _format_row(self, row):
        if 'doc' in row and row['doc']:
            row['doc'] = Document(self.db, row['doc'])
        return row

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return (self._format_row(x) for x in self._rows)

    def __getitem__(self, key):
        return self._format_row(self._rows[key])


class Paginator(TrombiObject):
    """
    Provides pseudo pagination of CouchDB documents calculated from
    the total_rows and offset of a CouchDB view as well as a user-
    defined page limit.
    """
    def __init__(self, db, limit=10):
        self._db = db
        self._limit = limit
        self.response = None
        self.count = 0
        self.start_index = 0
        self.end_index = 0
        self.num_pages = 0
        self.current_page = 0
        self.previous_page = 0
        self.next_page = 0
        self.rows = None
        self.has_next = False
        self.has_previous = False
        self.page_range = None
        self.start_doc_id = None
        self.end_doc_id = None

    def get_page(self, design_doc, viewname, callback,
            key=None, doc_id=None, forward=True, **kw):
        """
        On success, callback is called with this Paginator object as an
        argument that is fully populated with the page data requested.

        Use forward = True for paging forward, and forward = False for
        paging backwargs

        The combination of key/doc_id and forward is crucial.  When
        requesting to paginate forward the key/doc_id must be the built
        from the _last_ document on the current page you are moving forward
        from.  When paginating backwards, the key/doc_id must be built
        from the _first_ document on the current page.

        """
        def _really_callback(response):
            if response.error:
                # Send the received Database.view error to the callback
                self.error = response
                callback(self)
                return

            if forward:
                offset = response.offset
            else:
                offset = response.total_rows - response.offset - self._limit

            self.response = response
            self.count = response.total_rows
            self.start_index = offset
            self.end_index = response.offset + self._limit - 1
            self.num_pages = (self.count / self._limit) + 1
            self.current_page = (offset / self._limit) + 1
            self.previous_page = self.current_page - 1
            self.next_page = self.current_page + 1
            self.rows = [row['value'] for row in response]
            if not forward:
                self.rows.reverse()
            self.has_next = (offset + self._limit) < self.count
            self.has_previous = (offset - self._limit) >= 0
            self.page_range = [p for p in xrange(1, self.num_pages+1)]
            try:
                self.start_doc_id = self.rows[0]['_id']
                self.end_doc_id = self.rows[-1]['_id']
            except (IndexError, KeyError):
                # empty set
                self.start_doc_id = None
                self.end_doc_id = None
            callback(self)

        kwargs = {'limit': self._limit,
                  'descending': True}
        kwargs.update(kw)

        if 'startkey' not in kwargs:
            kwargs['startkey'] = key

        if kwargs['startkey'] and forward and doc_id:
            kwargs['start_doc_id'] = doc_id
        elif kwargs['startkey'] and not forward:
            kwargs['start_doc_id'] = doc_id if doc_id else ''
            kwargs['descending'] = False if kwargs['descending'] else True
            kwargs['skip'] = 1

        self._db.view(design_doc, viewname, _really_callback, **kwargs)


VALID_DB_NAME = re.compile(r'^[a-z][a-z0-9_$()+-/]*$')
