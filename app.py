import datetime
import statistics
import urllib

from flask import (Flask, flash, Markup, redirect, render_template, request,
                   Response, session, url_for)
from flask_bcrypt import Bcrypt
from markdown import markdown
from markdown.extensions.extra import ExtraExtension
from micawber import bootstrap_basic, parse_html
from micawber.cache import Cache as OEmbedCache
from playhouse.flask_utils import FlaskDB, get_object_or_404, object_list
from playhouse.sqlite_ext import *
from playhouse.migrate import *


from pygments.styles import STYLE_MAP


# Blog configuration values.

# You may consider using a one-way hash to generate the password, and then
# use the hash again in the login view to perform the comparison. This is just
# for simplicity.
APP_DIR = os.path.dirname(os.path.realpath(__file__))

# The playhouse.flask_utils.FlaskDB object accepts database URL configuration.
DATABASE = 'sqliteext:///%s' % os.path.join(APP_DIR, 'blog.db')
DEBUG = False

# The secret key is used internally by Flask to encrypt session data stored
# in cookies. Make this unique for your app.
SECRET_KEY = 'shhh, secret!'

# This is used by micawber, which will attempt to generate rich media
# embedded objects with maxwidth=800.
SITE_WIDTH = 800


# Create a Flask WSGI app and configure it using values from the module.
app = Flask(__name__)
app.config.from_object(__name__)

# FlaskDB is a wrapper for a peewee database that sets up pre/post-request
# hooks for managing database connections.
flask_db = FlaskDB(app)

# The `database` is the actual peewee database, as opposed to flask_db which is
# the wrapper.
database = flask_db.database
migrator = SqliteMigrator(database)

# password encryption
bcrypt = Bcrypt(app)

# Configure micawber with the default OEmbed providers (YouTube, Flickr, etc).
# We'll use a simple in-memory cache so that multiple requests for the same
# video don't require multiple network requests.
oembed_providers = bootstrap_basic(OEmbedCache())

tag_count_dict = {}


class Entry(flask_db.Model):
    title = CharField()
    slug = CharField()
    content = TextField()
    published = BooleanField(index=True)
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)

    @property
    def html_content(self):
        """
        Generate HTML representation of the markdown-formatted blog entry,
        and also convert any media URLs into rich media objects such as video
        players or images.
        """
        # hilite = CodeHiliteExtension(linenums=False, noclasses=True, pygments_style="native")
        extras = ExtraExtension()
        markdown_content = markdown(self.content, extensions=[extras])
        oembed_content = parse_html(
            markdown_content,
            oembed_providers,
            urlize_all=True,
            maxwidth=app.config['SITE_WIDTH'])
        return Markup(oembed_content)

    # returns array of tag labels
    def get_tags(self):
        tag_array = []
        if self.tag_entries:
            for entry in self.tag_entries:
                tag_array.append(entry.tag.label)
            return tag_array
        else:
            return None

    # returns tag list in comma-separated string format
    def tag_string_list(self):
        tags = self.get_tags()
        if tags:
            return ", ".join(tags)
        else:
            return ""



    def save(self, *args, **kwargs):
        # Generate a URL-friendly representation of the entry's title.
        if not self.slug:
            self.slug = re.sub('[^\w]+', '-', self.title.lower()).strip('-')
        ret = super(Entry, self).save(*args, **kwargs)

        # Store search content.
        self.update_search_index()

        return ret

    def update_search_index(self):
        # Create a row in the FTSEntry table with the post content. This will
        # allow us to use SQLite's awesome full-text search extension to
        # search our entries.
        try:
            fts_entry = FTSEntry.get(FTSEntry.entry_id == self.id)
        except FTSEntry.DoesNotExist:
            fts_entry = FTSEntry(entry_id=self.id)
            fts_entry.content = '\n'.join((self.title, self.content))
            fts_entry.save(force_insert=True)

        else:
            fts_entry.content = '\n'.join((self.title, self.content))
            FTSEntry.update(content=fts_entry.content).where(FTSEntry.entry_id == self.id)

    @classmethod
    def public(cls):
        return Entry.select().where(Entry.published == True)

    @classmethod
    def drafts(cls):
        return Entry.select().where(Entry.published == False)

    @classmethod
    def search(cls, query):
        words = [word.strip() for word in query.split() if word.strip()]
        if not words:
            # Return an empty query.
            return Entry.select().where(Entry.id == 0)
        else:
            search = ' '.join(words)

        # Query the full-text search index for entries matching the given
        # search query, then join the actual Entry data on the matching
        # search result.
        return (FTSEntry
                .select(
                    FTSEntry,
                    Entry,
                    FTSEntry.rank().alias('score'))
                .join(Entry, on=(FTSEntry.entry_id == Entry.id).alias('entry'))
                .where(
                    (Entry.published == True) &
                    (FTSEntry.match(search)))
                .order_by(SQL('score').desc()))


class FTSEntry(FTSModel):
    entry_id = IntegerField(Entry)
    content = TextField()

    class Meta:
        database = database


class Tag(flask_db.Model):
    label = CharField(unique=True)
    level = CharField(null=True)


class BlogEntryTags(flask_db.Model):
    blog_entry = ForeignKeyField(Entry, related_name="tag_entries")
    tag = ForeignKeyField(Tag)

    class Meta:
        primary_key = CompositeKey('blog_entry', 'tag')


def login_required(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        if session.get('logged_in'):
            return fn(*args, **kwargs)
        return redirect(url_for('login', next=request.path))
    return inner


@app.route('/login/', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next') or request.form.get('next')
    if request.method == 'POST' and request.form.get('password'):
        password = request.form.get('password')
        f = open('admin_password_hash', 'r')
        hash = f.read()
        if bcrypt.check_password_hash(hash, password):
            session['logged_in'] = True
            session.permanent = True  # Use cookie to store session.
            flash('You are now logged in.', 'success')
            return redirect(next_url or url_for('index'))
        else:
            flash('Incorrect password.', 'danger')
    return render_template('login.html', next_url=next_url)


@app.route('/logout/', methods=['GET', 'POST'])
def logout():
    if request.method == 'POST':
        session.clear()
        return redirect(url_for('login'))
    return render_template('logout.html')


@app.route('/')
def index():

    see_all_posts = request.args.get('allEntries')
    search_query = request.args.get('q')

    if see_all_posts:
        query = Entry.public().order_by(Entry.timestamp.desc())
        return object_list(
            'entries.html',
            query,
            search=search_query,
            check_bounds=False)

    elif search_query:
        query = Entry.search(search_query)
    # else:
    #     query = Entry.public().order_by(Entry.timestamp.desc())

        # The `object_list` helper will take a base query and then handle
        # paginating the results if there are more than 20. For more info see
        # the docs:
        # http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#object_list
        return object_list(
            'entries.html',
            query,
            search=search_query,
            check_bounds=False)

    else:
        return render_template('index.html')


@app.route('/create/', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        if request.form.get('title') and request.form.get('content'):
            entry = Entry.create(
                title=request.form['title'],
                content=request.form['content'],
                published=False)

            if request.form.get('publish'):
                entry.published = True

            # loop through creating tags
            update_tags(request, entry)
            entry.save()

            if request.form.get('preview'):
                return redirect(url_for('preview', slug=entry.slug))

            elif request.form.get('save'):
                flash('Entry saved successfully.', 'success')
                return redirect(url_for('edit', slug=entry.slug))

            elif request.form.get('publish'):
                flash('Entry created successfully.', 'success')
                return redirect(url_for('detail', slug=entry.slug))

        else:
            flash('Title and Content are required.', 'danger')
    return render_template('create.html')


@app.route('/drafts/')
@login_required
def drafts():
    query = Entry.drafts().order_by(Entry.timestamp.desc())
    return object_list('entries.html', query, check_bounds=False)


@app.route('/<slug>/')
def detail(slug):

    print STYLE_MAP.keys()


    if session.get('logged_in'):
        query = Entry.select()
    else:
        query = Entry.public()
    entry = get_object_or_404(query, Entry.slug == slug)
    return render_template('detail.html', entry=entry)


@app.route('/<slug>/preview')
def preview(slug):

    print STYLE_MAP.keys()

    if session.get('logged_in'):
        query = Entry.select()
    else:
        query = Entry.public()
    entry = get_object_or_404(query, Entry.slug == slug)
    return render_template('preview.html', entry=entry)


@app.route('/tag/<label>/')
def tag(label):
    query = Entry.select()\
        .join(BlogEntryTags)\
        .join(Tag)\
        .where(Tag.label == label).order_by(Entry.timestamp.desc())
    return object_list('tag.html', query, label=label)


@app.context_processor
def inject_tags():
    tags = Tag.select()
    if not session.get('logged_in'):
        published_tags = []
        for tag in tags:
            published = Entry.select() \
                .join(BlogEntryTags) \
                .join(Tag) \
                .where(Tag.label == tag.label, Entry.published == True).count()
            if published > 0:
                published_tags.append(tag)
        return dict(tags=sorted(published_tags, key=lambda tag: tag.label))

    return dict(tags=sorted(tags, key=lambda tag : tag.label))


@app.context_processor
def inject_entries():
    if not session.get('logged_in'):
        published = [entry for entry in Entry.select() if entry.published]
        return dict(entries=sorted(published, key=lambda entry : entry.timestamp, reverse=True))
    return dict(entries=sorted(Entry.select(), key=lambda entry: entry.timestamp, reverse=True))


@app.template_filter()
def get_recent(entries):

    return entries[:5]

# TAG_RANK = ("tagRank10","tagRank9",
# "tagRank8","tagRank7","tagRank6",
# "tagRank5","tagRank4","tagRank3",
# "tagRank2","tagRank1")

TAG_RANK = ("tagRank5","tagRank4","tagRank3",
"tagRank2","tagRank1")


@app.template_filter()
def rank_tags(tags):
    maxPosts = max(tag_count_dict.values())
    for tag in tags:
        tag.level = TAG_RANK[int(float(tag_count_dict.get(tag.label))/maxPosts*len(TAG_RANK))-1]

    return tags


@app.route('/<slug>/edit/', methods=['GET', 'POST'])
@login_required
def edit(slug):
    entry = get_object_or_404(Entry, Entry.slug == slug)

    if request.method == 'POST':

        if request.form.get('delete'):
            entry.delete_instance(recursive=True)

            # delete any tags if no entries in them anymore
            entryTags = entry.get_tags()
            if entryTags:
                for t in entryTags:
                    query = Entry.select() \
                        .join(BlogEntryTags) \
                        .join(Tag) \
                        .where(Tag.label == t)
                    if query.count() == 0:
                        tag_model = Tag.get(Tag.label == t)
                        tag_model.delete_instance()

                    count = tag_count_dict.get(t)
                    if count is not None and count > 0:
                        tag_count_dict[t] -= 1

            flash("Entry '" + slug + "' was deleted.", "success")
            return redirect(url_for('index'))

        elif request.form.get('title') and request.form.get('content'):
            entry.title = request.form['title']
            entry.content = request.form['content']
            entry.published = request.form.get('published') or False

            # loop through creating tags
            update_tags(request, entry)

            entry.save()

            if request.form.get('preview'):
                return redirect(url_for('preview', slug=entry.slug))

            elif request.form.get('save'):
                flash('Entry saved successfully.', 'success')

                if entry.published:
                    return redirect(url_for('detail', slug=entry.slug))
                else:
                    return redirect(url_for('edit', slug=entry.slug))
        else:
            flash('Title and Content are required.', 'danger')

    return render_template('edit.html', entry=entry)


def update_search_index(self):
    # Create a row in the FTSEntry table with the post content. This will
    # allow us to use SQLite's awesome full-text search extension to
    # search our entries.
    try:
        fts_entry = FTSEntry.get(FTSEntry.entry_id == self.id)
    except FTSEntry.DoesNotExist:
        fts_entry = FTSEntry(entry_id=self.id)
        fts_entry.content = '\n'.join((self.title, self.content))
        fts_entry.save(force_insert=True)

    else:
        # TODO: Bug in peewee.py line 4971 where force_insert=False
        # forces this workaround
        fts_entry.content = '\n'.join((self.title, self.content))
        FTSEntry.update(content=fts_entry.content).where(FTSEntry.entry_id == self.id)


def update_tags(request, entry):
    if request.form.get('tags'):
        # TODO: Better user input handling here
        tags = [tag.lower() for tag in request.form['tags'].split(", ")]

        # Delete any existing tags in current entry that do not appear in current form field
        if entry.get_tags():
            oldTags = [tag.lower() for tag in entry.get_tags()]
            toRemove = list(set(oldTags)-set(tags))
            for r in toRemove:
                tag_model = Tag.get(Tag.label == r)
                tagRelationship = BlogEntryTags.get(blog_entry=entry.id,
                                                    tag=tag_model.id)
                if tagRelationship:
                    tagRelationship.delete_instance()

                # remove tag entirely if no entries left
                query = Entry.select() \
                    .join(BlogEntryTags) \
                    .join(Tag) \
                    .where(Tag.label == r)
                if query.count() == 0:
                    tag_model = Tag.get(Tag.label == r)
                    tag_model.delete_instance()

                # remove from tag count dict
                count = tag_count_dict.get(r)
                if count is not None and count > 0:
                    tag_count_dict[r] -= 1



        for t in tags:

            # creates tag if doesn't exist yet
            try:
                tag_model = Tag.get(Tag.label == t)
            except Tag.DoesNotExist:
                tag_model = Tag.create(label=t)

            count = tag_count_dict.get(t)
            if count is None:
                tag_count_dict[t] = 1
            else:
                tag_count_dict[t] += 1

            # creates relationship if doesn't exist
            try:
                entry_tag = BlogEntryTags.get(blog_entry=entry.id,
                                              tag=tag_model.id)
            except BlogEntryTags.DoesNotExist:
                entry_tag = BlogEntryTags(blog_entry=entry.id, tag=tag_model.id)
                entry_tag.save(force_insert=True)


@app.template_filter('clean_querystring')
def clean_querystring(request_args, *keys_to_remove, **new_values):
    # We'll use this template filter in the pagination include. This filter
    # will take the current URL and allow us to preserve the arguments in the
    # querystring while replacing any that we need to overwrite. For instance
    # if your URL is /?q=search+query&page=2 and we want to preserve the search
    # term but make a link to page 3, this filter will allow us to do that.
    querystring = dict((key, value) for key, value in request_args.items())
    for key in keys_to_remove:
        querystring.pop(key, None)
    querystring.update(new_values)
    return urllib.urlencode(querystring)


@app.errorhandler(404)
def not_found(exc):
    return Response('<h3>Not found</h3>'), 404


def populateTagCloudDict():
    if not tag_count_dict:
        for tag in Tag.select():
            tag_count_dict[tag.label] = Entry.select() \
                .join(BlogEntryTags) \
                .join(Tag) \
                .where(Tag.label == tag.label).count()


def main():
    database.create_tables([Entry, FTSEntry, Tag, BlogEntryTags], safe=True)
    populateTagCloudDict()
    app.run(debug=True)


if __name__ == '__main__':
    main()
