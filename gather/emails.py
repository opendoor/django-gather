from django.contrib.sites.models import Site
from django.core import urlresolvers
from django.conf import settings
from django.template.loader import get_template
from django.template import Context
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, HttpResponseRedirect
from gather.models import Event
from gather.views import get_location
import requests
import logging
import json

logger = logging.getLogger(__name__)

def mailgun_send(mailgun_data):
	logger.debug("Mailgun send: %s" % mailgun_data)
	if settings.DEBUG:
		# When this is true you will see this message in the mailgun logs but
		# nothing will actually be delivered
		mailgun_data["o:testmode"] = "yes"
	resp = requests.post("https://api.mailgun.net/v2/%s/messages" % settings.LIST_DOMAIN,
		auth=("api", settings.MAILGUN_API_KEY),
		data=mailgun_data
	)
	logger.debug("Mailgun response: %s" % resp.text)
	return HttpResponse(status=200)

def new_event_notification(event, location):
	# notify the event admins
	admin_group = event.admin
	recipients = [admin.email for admin in admin_group.users.all()]
	event_short_title = event.title[0:50]
	if len(event.title) > 50:
		event_short_title = event_short_title + "..."
	subject = '[' + location.email_subject_prefix + ']' + " A new event has been created: %s" % event_short_title
	from_address = location.from_email()
	plaintext = get_template('emails/new_event_notify.txt')
	c = Context({
		'event': event,
		'creator': event.creator,
		'location': location,
		'location_name': location.name,
	})
	body_plain = plaintext.render(c)
	mailgun_data={"from": from_address,
			"to": recipients,
			"subject": subject,
			"text": body_plain,
		}
	return mailgun_send(mailgun_data)

def event_approved_notification(event, location):
	logger.debug("event_approved_notification")
	recipients = [organizer.email for organizer in event.organizers.all()]
	subject = '[' + location.email_subject_prefix + ']' + " Your event is ready to be published"
	from_address = location.from_email()
	plaintext = get_template('emails/event_approved_notify.txt')
	c = Context({
		'event': event,
		'domain' : Site.objects.get_current().domain,
		'location_name': location.name,
	})
	body_plain = plaintext.render(c)
	mailgun_data={"from": from_address,
		"to": recipients,
		"subject": subject,
		"text": body_plain,
	}
	return mailgun_send(mailgun_data)

def event_published_notification(event, location):
	logger.debug("event_published_notification")
	recipients = [organizer.email for organizer in event.organizers.all()]
	event_short_title = event.title[0:50]
	if len(event.title) > 50:
		event_short_title = event_short_title + "..."
	subject = '[' + location.email_subject_prefix + ']' + " Your event is now live: %s" % event_short_title
	from_address = location.from_email()
	plaintext = get_template('emails/event_published_notify.txt')
	c = Context({
		'event': event,
		'domain' : Site.objects.get_current().domain,
		'location_name': location.name,
	})
	body_plain = plaintext.render(c)
	mailgun_data={"from": from_address,
		"to": recipients,
		"subject": subject,
		"text": body_plain,
	}
	return mailgun_send(mailgun_data)

###############################################
########### Email Route Create ################

def create_route(route_name, route_pattern, path):
	mailgun_api_key = settings.MAILGUN_API_KEY
	list_domain = settings.LIST_DOMAIN
	# strip the initial slash
	forward_url = os.path.join(list_domain, path)
	forward_url = "https://" + forward_url
	print forward_url
	print list_domain
	expression = "match_recipient('%s')" % route_pattern
	print expression
	forward_url = "forward('%s')" % forward_url
	print forward_url
	return requests.post( "https://api.mailgun.net/v2/routes",
			auth=("api", mailgun_api_key),
			data={"priority": 1,
				"description": route_name,
				# the route pattern is a string but still needs to be quoted
				"expression": expression,
				"action": forward_url,
			}
	)

# TODO - We are goign to try and not create new routes
#def create_event_email(sender, instance, created, using, **kwargs):
#	if created == True:
#		# XXX TODO should probably hash the ID or name of the event so we're
#		# not info leaking here, if we care?
#		route_pattern = "event%d" % instance.id
#		route_name = 'Event %d' % instance.id
#		path = "events/message/"
#		resp = create_route(route_name, route_pattern, path)
#		print resp.text
#post_save.connect(create_event_email, sender=Event)

############################################
########### EMAIL ENDPOINTS ################

@csrf_exempt
def event_message(request, location_slug=None):
	''' new messages sent to event email addresses are posed to this view '''
	if not request.method == 'POST':
		return HttpResponseRedirect('/404')

	recipient = request.POST.get('recipient')
	from_address = request.POST.get('from')
	sender = request.POST.get('sender')
	subject = request.POST.get('subject')
	body_plain = request.POST.get('body-plain')
	body_html = request.POST.get('body-html')
	
	# get the event info and make sure the event exists
	# we know that the route is always in the form eventXX, where XX is the
	# event id.
	alias = recipient.split('@')[0]
	logger.debug("event_message: alias=%s" % alias)
	event = None
	try:
		event_id = int(alias[5:])
		logger.debug("event_message: event_id=%s" % event_id)
		event = Event.objects.get(id=event_id)
	except:
		pass
	if not event:
		logger.warn("Event (%s) not found.  Exiting quietly." % alias)
		return HttpResponse(status=200)

	# Do some sanity checkint so we don't mailbomb everyone
	header_txt = request.POST.get('message-headers')
	message_headers = json.loads(header_txt)
	message_header_keys = [item[0] for item in message_headers]
	# make sure this isn't an email we have already forwarded (cf. emailbombgate 2014)
	# A List-Id header will only be present if it has been added manually in
	# this function, ie, if we have already processed this message. 
	if request.POST.get('List-Id') or 'List-Id' in message_header_keys:
		logger.debug('List-Id header was found! Dropping message silently')
		return HttpResponse(status=200)
	# If 'Auto-Submitted' in message_headers or message_headers['Auto-Submitted'] != 'no':
	if 'Auto-Submitted' in message_header_keys: 
		logger.info('message appears to be auto-submitted. reject silently')
		return HttpResponse(status=200)

	# find the event organizers and admins
	organizers = event.organizers.all()
	location = get_location(location_slug)
	location_event_admin = EventAdminGroup.objects.get(location=location)
	admins = location_event_admin.users.all()

	# Build our bcc list 
	bcc_list = []
	for organizer in organizers:
		if organizer.email not in bcc_list:
			bcc_list.append(organizer.email)
	for admin in admins:
		if admin.email not in bcc_list:
			bcc_list.append(admin.email)

	# Make sure this person can post to our list
	if not from_address in bcc_list:
		logger.warn("From address (%s) not allowed.  Exiting quietly." % from_address)
		return HttpResponse(status=200)

	# prefix subject
	if subject.find('[Event Discussion') < 0:
		prefix = '[Event Discussion: %s] ' % event.slug[0:30]
		subject = prefix + subject

	# Add in footer
	event_url = urlresolvers.reverse('gather_view_event', args=(location.slug, event.id, event.slug))
	footer_msg = "You are receving this email because you are one of the organizers or an event admin at this location. Visit this event online at %s" % event_url
	body_plain = body_plain + "\n\n-------------------------------------------\n" + footer_msg
	body_html = body_html + "<br><br>-------------------------------------------<br>" + footer_msg

	# send the message 
	mailgun_data={"from": from_address,
		"to": [recipient, ],
		"bcc": bcc_list,
		"subject": subject,
		"text": body_plain,
		"html": body_html
	}
	return mailgun_send(mailgun_data)





