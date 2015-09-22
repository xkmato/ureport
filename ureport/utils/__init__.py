import copy
import json
import math
import time
from datetime import timedelta, datetime
from dash.orgs.models import Org
from dash.utils import temba_client_flow_results_serializer, datetime_to_ms
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.text import slugify
from django_redis import get_redis_connection
import pycountry
import pytz
from ureport.assets.models import Image, FLAG
from raven.contrib.django.raven_compat.models import client
from ureport.polls.models import Poll

GLOBAL_COUNT_CACHE_KEY = 'global_count'


def get_linked_orgs(authenticated=False):
    all_orgs = Org.objects.filter(is_active=True).order_by('name')

    linked_sites = list(getattr(settings, 'PREVIOUS_ORG_SITES', []))

    # populate a ureport site for each org so we can link off to them
    for org in all_orgs:
        host = org.build_host_link(authenticated)
        org.host = host
        if org.get_config('is_on_landing_page'):
            flag = Image.objects.filter(org=org, is_active=True, image_type=FLAG).first()
            if flag:
                linked_sites.append(dict(name=org.name, host=host, flag=flag.image.url, is_static=False))

    linked_sites_sorted = sorted(linked_sites, key=lambda k: k['name'].lower())

    return linked_sites_sorted


def substitute_segment(org, segment_in):
    if not segment_in:
        return segment_in

    segment = copy.deepcopy(segment_in)

    location = segment.get('location', None)
    if location == 'State':
        segment['location'] = org.get_config('state_label')
    elif location == 'District':
        segment['location'] = org.get_config('district_label')

    if org.get_config('is_global'):
        if "location" in segment:
            del segment["location"]
            if 'parent' in segment:
                del segment["parent"]
            segment["contact_field"] = org.get_config('state_label')
            segment["values"] = [elt.alpha2 for elt in pycountry.countries.objects]

    return json.dumps(segment)


def clean_global_results_data(org, results, segment):

    # for the global page clean the data translating country code to country name
    if org.get_config('is_global') and results and segment and 'location' in segment:
        for elt in results:
            country_code = elt['label']
            elt['boundary'] = country_code
            country_name = ""
            try:
                country = pycountry.countries.get(alpha2=country_code)
                if country:
                    country_name = country.name
            except KeyError:
                country_name = country_code
            elt['label'] = country_name

    return results


def fetch_contact_field_results(org, contact_field, segment):
    from ureport.polls.models import CACHE_ORG_FIELD_DATA_KEY, UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME
    from ureport.polls.models import UREPORT_RUN_FETCHED_DATA_CACHE_TIME

    start = time.time()
    print "Fetching  %s for %s with segment %s" % (contact_field, org.name, segment)

    cache_time = UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME
    if segment and segment.get('location', "") == "District":
        cache_time = UREPORT_RUN_FETCHED_DATA_CACHE_TIME

    try:
        segment = substitute_segment(org, segment)

        this_time = datetime.now()

        temba_client = org.get_temba_client()
        client_results = temba_client.get_results(contact_field=contact_field, segment=segment)

        results_data = temba_client_flow_results_serializer(client_results)
        cleaned_results_data = results_data

        print "Fetch took %ss" % (time.time() - start)

        key = CACHE_ORG_FIELD_DATA_KEY % (org.pk, slugify(unicode(contact_field)), slugify(unicode(segment)))
        cache.set(key, {'time': datetime_to_ms(this_time), 'results': cleaned_results_data}, cache_time)
    except:
        client.captureException()
        import traceback
        traceback.print_exc()


def get_contact_field_results(org, contact_field, segment):
    from ureport.polls.models import CACHE_ORG_FIELD_DATA_KEY

    subsituted_segment = substitute_segment(org, segment)

    key = CACHE_ORG_FIELD_DATA_KEY % (org.pk, slugify(unicode(contact_field)), slugify(unicode(subsituted_segment)))
    cache_value = cache.get(key, None)

    if cache_value:
        return cache_value['results']

    if segment and segment.get('location', "") == "District":
        fetch_contact_field_results(org, contact_field, segment)
        cache_value = cache.get(key, None)
        if cache_value:
            return cache_value['results']


def get_most_active_regions(org):
    cache_key = 'most_active_regions:%d' % org.id
    active_regions = cache.get(cache_key, None)

    if active_regions is None:
        regions = org.get_contact_field_results(org.get_config('gender_label'), dict(location="State"))
        active_regions = dict()

        if not regions:
            return []

        for region in regions:
            active_regions[region['label']] = region['set'] + region['unset']

        tuples = [(k, v) for k, v in active_regions.iteritems()]
        tuples.sort(key=lambda t: t[1], reverse=True)

        active_regions = [k for k, v in tuples]
        cache.set(cache_key, active_regions, 3600 * 24)

    if org.get_config('is_global'):
        active_regions = [pycountry.countries.get(alpha2=elt).name for elt in active_regions]

    return active_regions


def organize_categories_data(org, contact_field, api_data):

    cleaned_categories = []
    interval_dict = dict()
    now = timezone.now()
    # if we have the age_label; Ignore invalid years and make intervals
    if api_data and contact_field.lower() == org.get_config('born_label').lower():
        current_year = now.year

        for elt in api_data[0]['categories']:
            year_label = elt['label']
            # try:
            #     if len(year_label) == 4 and int(float(year_label)) > 1900:
            #         decade = int(math.floor((current_year - int(elt['label'])) / 10)) * 10
            #         key = "%s-%s" % (decade, decade+10)
            #         if interval_dict.get(key, None):
            #             interval_dict[key] += elt['count']
            #         else:
            #             interval_dict[key] = elt['count']
            # except ValueError:
            #     pass
            try:
                if year_label.lower() != 'why':
                    key = year_label
                    if interval_dict.get(key, None):
                        interval_dict[key] += elt['count']
                    else:
                        interval_dict[key] = elt['count']
            except ValueError:
                pass

        for obj_key in interval_dict.keys():
            cleaned_categories.append(dict(label=obj_key, count=interval_dict[obj_key] ))

        api_data[0]['categories'] = sorted(cleaned_categories, key=lambda k: int(k['label'].strip('s')[0]))

    elif api_data and contact_field.lower() == org.get_config('registration_label').lower():
        six_months_ago = now - timedelta(days=180)
        six_months_ago = six_months_ago - timedelta(six_months_ago.weekday())
        tz = pytz.timezone('UTC')

        for elt in api_data[0]['categories']:
            time_str = elt['label']

            # ignore anything like None as label
            if not time_str:
                continue

            parsed_time = tz.localize(datetime.strptime(time_str, '%Y-%m-%dT%H:%M:%SZ'))

            # this is in the range we care about
            if parsed_time > six_months_ago:
                # get the week of the year
                dict_key = parsed_time.strftime("%W")

                if interval_dict.get(dict_key, None):
                    interval_dict[dict_key] += elt['count']
                else:
                    interval_dict[dict_key] = elt['count']

        # build our final dict using week numbers
        categories = []
        start = six_months_ago
        while start < timezone.now():
            week_dict = start.strftime("%W")
            count = interval_dict.get(week_dict, 0)
            categories.append(dict(label=start.strftime("%m/%d/%y"), count=count))

            start = start + timedelta(days=7)

        api_data[0]['categories'] = categories

    elif api_data and contact_field.lower() == org.get_config('occupation_label').lower():

        for elt in api_data[0]['categories']:
            if len(cleaned_categories) < 9 and elt['label'] != "All Responses":
                cleaned_categories.append(elt)

        api_data[0]['categories'] = cleaned_categories

    return api_data

LOCK_POLL_RESULTS_KEY = 'lock:poll:%d:results'
LOCK_POLL_RESULTS_TIMEOUT = 60 * 15


def _fetch_org_polls_results(org, polls):

    r = get_redis_connection()

    for poll in polls:
        start = time.time()
        print "Fetching polls results for poll id(%d) - %s" % (poll.pk, poll.title)

        key = LOCK_POLL_RESULTS_KEY % poll.pk
        if not r.get(key):
            with r.lock(key, timeout=LOCK_POLL_RESULTS_TIMEOUT):
                poll.fetch_poll_results()
                print "Fetch results for poll id(%d) on %s took %ss" % (poll.pk, org.name, time.time() - start)


def fetch_main_poll_results(org):
    main_poll = Poll.get_main_poll(org)
    if main_poll:
        _fetch_org_polls_results(org, [main_poll])


def fetch_brick_polls_results(org):
    brick_polls = Poll.get_brick_polls(org)[:5]
    _fetch_org_polls_results(org, brick_polls)


def fetch_other_polls_results(org):
    other_polls = Poll.get_other_polls(org)
    _fetch_org_polls_results(org, other_polls)


def fetch_org_graph_data(org):
    for data_label in ['born_label', 'registration_label', 'occupation_label', 'gender_label']:
        c_field = org.get_config(data_label)
        if c_field:
            fetch_contact_field_results(org, c_field, None)
            if data_label == 'gender_label':
                fetch_contact_field_results(org, c_field, dict(location='State'))


def fetch_flows(org):
    start = time.time()
    print "Fetching flows for %s" % org.name

    try:
        from ureport.polls.models import CACHE_ORG_FLOWS_KEY, UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME

        this_time = datetime.now()

        temba_client = org.get_temba_client()
        flows = temba_client.get_flows()

        all_flows = dict()
        for flow in flows:
            if flow.rulesets:
                flow_json = dict()
                flow_json['uuid'] = flow.uuid
                flow_json['name'] = flow.name
                flow_json['participants'] = flow.participants
                flow_json['runs'] = flow.runs
                flow_json['completed_runs'] = flow.completed_runs
                flow_json['rulesets'] = [
                    dict(uuid=elt.uuid, label=elt.label, response_type=elt.response_type) for elt in flow.rulesets]

                all_flows[flow.uuid] = flow_json

        all_flows_key = CACHE_ORG_FLOWS_KEY % org.pk
        cache.set(all_flows_key,
                  {'time': datetime_to_ms(this_time), 'results': all_flows},
                  UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME)
    except:
        client.captureException()
        import traceback
        traceback.print_exc()

    print "Fetch %s flows took %ss" % (org.name, time.time() - start)


def get_flows(org):
    from ureport.polls.models import CACHE_ORG_FLOWS_KEY
    cache_value = cache.get(CACHE_ORG_FLOWS_KEY % org.pk, None)
    if cache_value:
        return cache_value['results']

    return dict()


def fetch_reporter_group(org):
    start = time.time()
    print "Fetching reporter group for %s" % org.name
    try:
        from ureport.polls.models import CACHE_ORG_REPORTER_GROUP_KEY, UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME

        this_time = datetime.now()

        reporter_group = org.get_config('reporter_group')
        if reporter_group:
            temba_client = org.get_temba_client()
            groups = temba_client.get_groups(name=reporter_group)

            key = CACHE_ORG_REPORTER_GROUP_KEY % (org.pk, slugify(unicode(reporter_group)))
            group_dict = dict()
            if groups:
                group = groups[0]
                group_dict = dict(size=group.size, name=group.name, uuid=group.uuid)
            cache.set(key,
                      {'time': datetime_to_ms(this_time), 'results': group_dict},
                      UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME)
    except:
        client.captureException()
        import traceback
        traceback.print_exc()
    # delete the global count cache to force a recalculate at the end
    cache.delete(GLOBAL_COUNT_CACHE_KEY)

    print "Fetch %s reporter group took %ss" % (org.name, time.time() - start)


def get_reporter_group(org):
    from ureport.polls.models import CACHE_ORG_REPORTER_GROUP_KEY

    reporter_group = org.get_config('reporter_group')

    if reporter_group:
        key = CACHE_ORG_REPORTER_GROUP_KEY % (org.pk, slugify(unicode(reporter_group)))
        cache_value = cache.get(key, None)
        if cache_value:
            group_dict = cache_value['results']
            if group_dict:
                return group_dict

    return dict()


def fetch_old_sites_count():
    import requests, re
    from ureport.polls.models import UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME

    start = time.time()
    this_time = datetime.now()
    linked_sites = list(getattr(settings, 'PREVIOUS_ORG_SITES', []))

    for site in linked_sites:
        count_link = site.get('count_link', "")
        if count_link:
            try:
                response = requests.get(count_link)
                response.raise_for_status()

                count = int(re.search(r'\d+', response.content).group())
                key = "org:%s:reporters:%s" % (site.get('name').lower(), 'old-site')
                cache.set(key,
                          {'time': datetime_to_ms(this_time), 'results': dict(size=count)},
                          UREPORT_ASYNC_FETCHED_DATA_CACHE_TIME)
            except:
                import traceback
                traceback.print_exc()

    # delete the global count cache to force a recalculate at the end
    cache.delete(GLOBAL_COUNT_CACHE_KEY)

    print "Fetch old sites counts took %ss" % (time.time() - start)


def get_global_count():

    count_cached_value = cache.get(GLOBAL_COUNT_CACHE_KEY, None)
    if count_cached_value:
        return count_cached_value

    try:
        reporter_counter_keys = cache.keys('org:*:reporters:*')

        cached_values = [cache.get(key) for key in reporter_counter_keys]

        count = sum([elt['results'].get('size', 0) for elt in cached_values if elt.get('results', None)])

        # cached for 10 min
        cache.set(GLOBAL_COUNT_CACHE_KEY, count, 60 * 10)
    except AttributeError:
        import traceback
        traceback.print_exc()
        count = '__'

    return count


Org.get_contact_field_results = get_contact_field_results
Org.get_most_active_regions = get_most_active_regions
Org.organize_categories_data = organize_categories_data
Org.get_flows = get_flows
Org.get_reporter_group = get_reporter_group
Org.substitute_segment = substitute_segment
