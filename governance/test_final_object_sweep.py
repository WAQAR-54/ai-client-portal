"""Final focused sweep: EVERY chat, billing and notification route that takes an object identifier is called with
identifiers of an object the caller does not own, and none may reveal it or change it.

governance/test_object_authorization.py proves specific routes by hand. This walks the URL configuration, so a
route added later (or one the hand-written tests missed) cannot slip through: a route with an identifier this
sweep does not know how to fill fails the test until it is mapped or deliberately listed as public."""

from django.test import override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from accounts.models import Department
from billing.models import Invoice, RefundRequest
from chat.models import ArenaComparison, Conversation, Message, Project, PromptTemplate
from governance.test_object_authorization import DENIED, ObjectFixtures
from notifications.models import Notification
from providers.models import Provider, ProviderModel

APPS = ("chat", "billing", "notifications")
# Identifier names this sweep understands, and the (alice-owned) object each one points at.
KNOWN_PARAMS = {
    "conversation_id",
    "message_id",
    "comparison_id",
    "project_id",
    "template_id",
    "invoice_id",
    "notification_id",
    "request_id",
    "team_id",
    "plan_id",
    "department_id",
    "token",
    "doc_format",
}
# Routes whose only identifier is an unguessable public token (a shared invoice link, an email-open pixel): public
# by design, and answering 404 for a wrong token is what they are tested for.
PUBLIC_TOKEN_ROUTES = {"public_invoice", "public_invoice_pdf", "track_email_open"}
SECRET_MARKERS = (
    "alice secret question",
    "alice secret answer",
    "Alice private plan",
    "alice@example.com",
    "alice-contract",
)


def object_routes():
    """[(url_name, [param names])] for every route of APPS that has a path parameter."""
    found = []

    def walk(patterns, namespace=""):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                inner = pattern.namespace or ""
                walk(pattern.url_patterns, f"{namespace}{inner}:" if inner else namespace)
            elif isinstance(pattern, URLPattern) and pattern.name:
                names = list(pattern.pattern.converters)
                if names and namespace.rstrip(":") in APPS:
                    found.append((f"{namespace}{pattern.name}", names))

    walk(get_resolver().url_patterns)
    return found


class FinalObjectSweepTests(ObjectFixtures):
    def setUp(self):
        super().setUp()
        self.reply_b = Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="b")
        openai = Provider.objects.get(slug="openai")
        model_a = ProviderModel.objects.create(provider=openai, model_id="sweep-a")
        model_b = ProviderModel.objects.create(provider=openai, model_id="sweep-b")
        self.comparison = ArenaComparison.objects.create(
            conversation=self.conversation,
            user_message=self.user_message,
            response_a=self.reply,
            response_b=self.reply_b,
            model_a=model_a,
            model_b=model_b,
        )
        self.refund = RefundRequest.objects.create(
            invoice=self.invoice, requested_by=self.alice, reason="alice secret question", requested_amount=10
        )
        self.values = {
            "conversation_id": self.conversation.pk,
            "message_id": self.user_message.pk,
            "comparison_id": self.comparison.pk,
            "project_id": self.project.pk,
            "template_id": self.template.pk,
            "invoice_id": self.invoice.pk,
            "notification_id": self.notification.pk,
            "request_id": self.refund.pk,
            "team_id": self.team_x.pk,
            "plan_id": self.invoice.plan_id,
            "department_id": self.dept_x.pk,
            "token": self.pending.stream_token,
            "doc_format": "docx",
        }
        self.snapshot = self.state()

    def state(self):
        return {
            "conversations": list(Conversation.all_objects.values_list("pk", "is_deleted", "is_pinned", "project_id")),
            "messages": list(Message.objects.order_by("pk").values_list("pk", "content", "attachment")),
            "projects": list(Project.objects.values_list("pk", "name")),
            "templates": list(PromptTemplate.objects.values_list("pk", "name")),
            "invoices": list(Invoice.objects.values_list("pk", "status")),
            "refunds": list(RefundRequest.objects.values_list("pk", "status")),
            "departments": list(Department.objects.values_list("pk", "name")),
            "notifications": list(Notification.objects.values_list("pk", "is_read")),
        }

    def test_the_sweep_actually_covers_the_routes_that_take_identifiers(self):
        names = {name for name, _params in object_routes()}
        for expected in (
            "chat:chat_conversation",
            "chat:download_attachment",
            "chat:stream_message",
            "chat:pick_arena_winner",
            "chat:rename_project",
            "billing:invoice_proof",
            "billing:submit_payment_proof",
            "notifications:mark_read",
        ):
            self.assertIn(expected, names)

    def test_every_identifier_a_route_takes_is_understood_or_the_route_is_public_by_design(self):
        unknown = {
            (name, tuple(p for p in params if p not in KNOWN_PARAMS))
            for name, params in object_routes()
            if name.split(":")[1] not in PUBLIC_TOKEN_ROUTES and any(p not in KNOWN_PARAMS for p in params)
        }
        self.assertEqual(unknown, set(), "map these identifiers in KNOWN_PARAMS/values, or list the route as public")

    @override_settings(DEBUG=False)
    def test_nobody_else_can_reach_or_change_alices_objects_through_any_identifier_route(self):
        self.client.raise_request_exception = False
        tried = 0
        for actor in (self.bob, self.admin_y, self.manager_y):
            self.client.force_login(actor)
            for name, params in object_routes():
                if name.split(":")[1] in PUBLIC_TOKEN_ROUTES:
                    continue
                url = reverse(name, kwargs={p: self.values[p] for p in params})
                for method in ("get", "post"):
                    response = getattr(self.client, method)(url, {"content": "x", "name": "hijack", "rating": "up"})
                    tried += 1
                    body = (
                        response.content.decode("utf-8", "ignore")
                        if not response.streaming
                        else "".join(chunk.decode("utf-8", "ignore") for chunk in response.streaming_content)
                    )
                    allowed = DENIED + (405, 400, 429)
                    self.assertIn(
                        response.status_code, allowed, f"{actor.role} {method.upper()} {name} -> {response.status_code}"
                    )
                    for marker in SECRET_MARKERS:
                        self.assertNotIn(marker, body, f"{actor.role} {method.upper()} {name} leaked {marker!r}")
        self.assertGreater(tried, 100)
        self.assertEqual(self.state(), self.snapshot, "a refused request changed something")

    def test_a_query_parameter_cannot_pull_in_someone_elses_message(self):
        """?artifact=<id> is a deep link into the CURRENT conversation: another user's id must not resolve."""
        bob_conversation = Conversation.objects.create(user=self.bob, title="bob chat")
        self.client.force_login(self.bob)
        for param in ("artifact", "message_id", "conversation_id", "starter", "project_id"):
            body = self.client.get(
                reverse("chat:chat_conversation", kwargs={"conversation_id": bob_conversation.pk}),
                {param: self.reply.pk},
            ).content.decode()
            self.assertNotIn("alice secret answer", body, param)

    def test_a_posted_identifier_for_someone_elses_object_is_ignored(self):
        bob_conversation = Conversation.objects.create(user=self.bob)
        self.client.force_login(self.bob)
        response = self.client.post(
            reverse("chat:post_message", kwargs={"conversation_id": bob_conversation.pk}),
            {
                "content": "hello",
                "message_id": self.reply.pk,
                "conversation_id": self.conversation.pk,
                "project_id": self.project.pk,
            },
        )
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(Message.objects.filter(conversation=self.conversation).count(), 4)  # alice's chat untouched
        bob_conversation.refresh_from_db()
        self.assertIsNone(bob_conversation.project_id)

    def test_the_owner_still_reaches_their_own_objects_through_the_same_routes(self):
        """The sweep would pass trivially if the routes were simply broken for everyone."""
        self.client.force_login(self.alice)
        for name in ("chat:chat_conversation", "chat:download_attachment", "chat:export_conversation_markdown"):
            params = {p: self.values[p] for p in next(p for n, p in object_routes() if n == name)}
            self.assertEqual(self.client.get(reverse(name, kwargs=params)).status_code, 200, name)
        self.assertEqual(
            self.client.get(reverse("billing:invoice_proof", kwargs={"invoice_id": self.invoice.pk})).status_code, 200
        )

    def test_department_a_admin_reaches_department_a_but_not_department_b(self):
        invoice_b = Invoice.objects.create(
            department=self.dept_y,
            recipient_user=self.bob,
            plan=self.invoice.plan,
            issue_date=self.invoice.issue_date,
            due_date=self.invoice.due_date,
            currency="USD",
            subtotal=self.invoice.subtotal,
            tax_rate=self.invoice.tax_rate,
            tax_amount=self.invoice.tax_amount,
            total=self.invoice.total,
        )
        self.client.force_login(self.admin_x)
        self.assertEqual(
            self.client.get(reverse("billing:invoice_detail", kwargs={"invoice_id": self.invoice.pk})).status_code, 200
        )
        self.assertIn(
            self.client.get(reverse("billing:invoice_detail", kwargs={"invoice_id": invoice_b.pk})).status_code, DENIED
        )
        self.assertIn(
            self.client.post(reverse("billing:delete_invoice", kwargs={"invoice_id": invoice_b.pk})).status_code, DENIED
        )
        self.assertTrue(Invoice.objects.filter(pk=invoice_b.pk).exists())
