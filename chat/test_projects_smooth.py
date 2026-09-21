"""Projects in the chat sidebar: every action answers with only what changed (no conversation-list re-render),
counts stay fresh, and the markup carries what the page's JS relies on."""

import json
import re

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from chat.models import Conversation, Project


class ProjectResponsesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="p@example.com", password="pw12345!")
        self.client.login(email="p@example.com", password="pw12345!")

    def test_create_and_rename_answer_with_only_the_projects_section(self):
        created = self.client.post(reverse("chat:create_project"), {"name": "Website"})
        self.assertEqual(created.status_code, 200)
        self.assertContains(created, 'id="projects-section-container"')
        self.assertContains(created, "Website")
        self.assertNotContains(created, "conv-list-container")
        self.assertTemplateNotUsed(created, "chat/_conversation_list.html")

        project = Project.objects.get(user=self.user)
        renamed = self.client.post(reverse("chat:rename_project", kwargs={"project_id": project.id}), {"name": "Site"})
        self.assertContains(renamed, ">Site<")
        self.assertNotContains(renamed, "conv-list-container")

    def test_delete_names_the_project_for_the_page_and_keeps_its_conversations(self):
        project = Project.objects.create(user=self.user, name="Doomed")
        conversation = Conversation.objects.create(user=self.user, project=project)

        response = self.client.post(reverse("chat:delete_project", kwargs={"project_id": project.id}))

        self.assertEqual(json.loads(response["HX-Trigger"]), {"portalProjectDeleted": {"id": project.id}})
        self.assertTemplateNotUsed(response, "chat/_conversation_list.html")
        self.assertNotContains(response, "Doomed")
        conversation.refresh_from_db()
        self.assertIsNone(conversation.project)

    def test_move_swaps_only_that_row_and_refreshes_the_counts(self):
        project = Project.objects.create(user=self.user, name="Q3")
        conversation = Conversation.objects.create(user=self.user, title="Budget talk")
        other = Conversation.objects.create(user=self.user, title="Untouched")

        response = self.client.post(
            reverse("chat:move_conversation_to_project", kwargs={"conversation_id": conversation.id}),
            {"project_id": project.id},
        )

        self.assertContains(response, 'class="conv-item')
        self.assertContains(response, f'data-project-id="{project.id}"')
        self.assertContains(response, "Budget talk")
        self.assertNotContains(response, "Untouched")  # the rest of the list is not re-sent
        self.assertNotContains(response, 'id="conv-list-container"')  # (a row's own pin/delete forms target it)
        self.assertContains(response, 'id="projects-section-container"')
        self.assertContains(response, '<span class="project-item-count">1</span>', html=False)
        self.assertEqual(response.content.decode().count('<li class="conv-item'), 1)
        del other

    def test_move_back_out_clears_the_marker_and_the_count(self):
        project = Project.objects.create(user=self.user, name="Q3")
        conversation = Conversation.objects.create(user=self.user, project=project)

        response = self.client.post(
            reverse("chat:move_conversation_to_project", kwargs={"conversation_id": conversation.id}),
            {"project_id": ""},
        )

        self.assertContains(response, 'data-project-id=""')
        self.assertContains(response, '<span class="project-item-count">0</span>', html=False)

    def test_deleting_a_conversation_refreshes_its_project_count(self):
        project = Project.objects.create(user=self.user, name="Q3")
        keep = Conversation.objects.create(user=self.user, project=project)
        gone = Conversation.objects.create(user=self.user, project=project)

        response = self.client.post(reverse("chat:delete_conversation", kwargs={"conversation_id": gone.id}))

        self.assertContains(response, 'id="conv-list-container"')  # the list is still returned...
        self.assertContains(response, 'id="projects-section-container"')  # ...with the section beside it
        self.assertContains(response, '<span class="project-item-count">1</span>', html=False)
        del keep

    def test_the_swapped_in_forms_carry_a_real_csrf_token(self):
        """The fragment replaces the section's forms: a missing token would 403 every action taken after the swap."""
        Project.objects.create(user=self.user, name="Website")
        response = self.client.post(reverse("chat:create_project"), {"name": "Another"})
        tokens = re.findall(r'name="csrfmiddlewaretoken" value="([^"]*)"', response.content.decode())
        self.assertGreaterEqual(len(tokens), 4)  # new-project form + a rename and a delete form per project
        self.assertTrue(all(len(token) >= 32 and token != "NOTPROVIDED" for token in tokens), tokens)

    def test_another_users_project_cannot_be_read_through_any_of_these_responses(self):
        theirs = User.objects.create_user(email="theirs@example.com", password="pw12345!")
        Project.objects.create(user=theirs, name="Secret plans")

        response = self.client.post(reverse("chat:create_project"), {"name": "Mine"})

        self.assertNotContains(response, "Secret plans")


class ProjectMarkupTests(TestCase):
    """What the sidebar JS depends on is in the markup."""

    def setUp(self):
        self.user = User.objects.create_user(email="m@example.com", password="pw12345!")
        self.client.login(email="m@example.com", password="pw12345!")

    def test_the_new_project_form_blocks_an_empty_name_and_does_not_swap_the_list(self):
        html = self.client.get(reverse("chat:chat_home")).content.decode()
        form = html[html.index('id="new-project-form"') :]
        form = form[: form.index("</form>")]
        self.assertIn("required", form)
        self.assertIn('pattern=".*\\S.*"', form)  # whitespace-only is not a name
        self.assertIn('hx-swap="none"', form)

    def test_an_empty_projects_list_says_so(self):
        self.assertContains(self.client.get(reverse("chat:chat_home")), "No projects yet")

    def test_rows_are_keyboard_reachable_and_rename_is_in_place(self):
        Project.objects.create(user=self.user, name="Website")
        html = self.client.get(reverse("chat:chat_home")).content.decode()
        row = html[html.index('class="project-item"') :]
        row = row[: row.index("projects-empty") if "projects-empty" in row else len(row)]
        self.assertIn('role="button" tabindex="0"', row)
        self.assertIn("portalStartRenameProject(this)", row)
        self.assertIn('class="project-rename-form"', row)
        self.assertNotIn("prompt(", html[html.index("function portalStartRenameProject") :][:800])

    def test_a_conversations_move_menu_is_built_from_the_live_project_list(self):
        Conversation.objects.create(user=self.user, title="One")
        html = self.client.get(reverse("chat:chat_home")).content.decode()
        # Even with no project at render time the row has the menu shell; the choices are built when it opens.
        self.assertIn('class="export-menu conv-move-menu"', html)
        self.assertIn("data-move-url=", html)
        self.assertIn('form.setAttribute("hx-target", "closest .conv-item")', html)
        self.assertIn("button.textContent = label", html)  # a project name is inserted as text, never as markup

    def test_new_conversation_forms_are_filled_with_the_selected_project_by_the_page(self):
        html = self.client.get(reverse("chat:chat_home")).content.decode()
        self.assertIn("portal.projectFilter", html)
        self.assertIn('field.name = "project_id"', html)
        self.assertIn(reverse("chat:create_conversation"), html)
