#include "sections/connection-settings.h"
#include "sections/connection_info.h"
#include "sections/helpers.h"
#include "actions/profile-model.h"
#include "data/labels.h"
#include <string.h>

typedef struct _Editor Editor;
typedef enum { FIELD_TEXT, FIELD_SWITCH, FIELD_CHOICE } FieldKind;
typedef struct {
  GtkWidget *widget, *row, *undo;
  FieldKind kind;
  char *original;
} Field;
typedef struct {
  GtkWidget *method, *addresses, *gateway, *dns, *automatic_dns, *fields, *summary;
  const char *const *methods;
  const char *const *labels;
} IpFields;
struct _Editor {
  NetworkSidebarActions *actions;
  NMClient *client;
  AdwNavigationView *view;
  AdwNavigationPage *page;
  NMRemoteConnection *profile;
  NMConnection *baseline, *submitted;
  NMActiveConnection *active;
  guint64 version;
  guint refresh_timer;
  GPtrArray *fields;
  char *custom_mac, *device_mac;
  gboolean building, saving, dirty;
  GtkWidget *name, *ssid, *autoconnect, *metered, *hidden, *password, *mac;
  GtkWidget *security, *identity, *anonymous, *domain, *eap_group, *eap_method, *ca;
  GtkWidget *body, *error, *save, *footer, *change_count, *status, *connect;
  GtkWidget *live, *diagnostics;
  IpFields ip4, ip6;
};

static const char *const methods4[] = { "auto", "manual", "link-local", "shared", "disabled", NULL };
static const char *const methods6[] = { "auto", "manual", "dhcp", "link-local", "shared", "disabled", "ignore", NULL };
static const char *const labels4[] = { "DHCP", "Manual", "Link-local", "Shared", "Disabled", NULL };
static const char *const labels6[] = { "Automatic", "Manual", "DHCP only", "Link-local", "Shared", "Disabled", "Unmanaged", NULL };
/* Supplied by the NixOS profile from the same setting as NetworkManager. */
#define DEFAULT_WIFI_MAC_ADDRESS "@defaultWifiMacAddress@"
#define USES_IWD @usesIwd@
#if USES_IWD
static const char *const mac_values[] = { "stable-ssid", "permanent", "random" };
#else
static const char *const mac_values[] = { "stable-ssid", "permanent", "random", "preserve", "stable" };
#endif

static const char *effective_mac_policy(NMSettingWireless *wireless)
{
  const char *mac = nm_setting_wireless_get_cloned_mac_address(wireless);
#if USES_IWD
  /* NM only mirrors "random" and literal addresses to iwd. Other keywords
   * fall back to iwd's global per-network policy. */
  if (g_strcmp0(mac, "permanent") == 0 || g_strcmp0(mac, "preserve") == 0 || g_strcmp0(mac, "stable") == 0)
    return DEFAULT_WIFI_MAC_ADDRESS;
#endif
  return mac && *mac ? mac : DEFAULT_WIFI_MAC_ADDRESS;
}

static char *profile_device_mac(NMClient *client, NMConnection *connection)
{
  const GPtrArray *devices = nm_client_get_devices(client);
  const char *address = NULL;
  for (guint i = 0; devices && i < devices->len; i++) {
    NMDevice *device = g_ptr_array_index(devices, i);
    if (!NM_IS_DEVICE_WIFI(device) || !nm_device_connection_compatible(device, connection, NULL)) continue;
    if (address) return NULL; /* Don't guess between multiple compatible adapters. */
    address = nm_device_wifi_get_permanent_hw_address(NM_DEVICE_WIFI(device));
  }
  return g_strdup(address);
}

static GtkWidget *label_new(const char *text)
{
  GtkWidget *label = gtk_label_new(text);
  gtk_label_set_xalign(GTK_LABEL(label), 0);
  return label;
}

/* A single-line label/control row replaces the tall title/subtitle cards. */
static GtkWidget *row_new(GtkWidget *box, const char *title)
{
  GtkWidget *row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
  GtkWidget *label = label_new(title);
  gtk_widget_add_css_class(row, "profile-field");
  gtk_widget_add_css_class(label, "dim-label");
  gtk_widget_set_size_request(label, 94, -1);
  gtk_box_append(GTK_BOX(row), label);
  gtk_box_append(GTK_BOX(box), row);
  return row;
}

static GtkWidget *group(GtkWidget *content, const char *title, const char *description)
{
  GtkWidget *section = gtk_box_new(GTK_ORIENTATION_VERTICAL, 6);
  GtkWidget *card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  if (title) {
    GtkWidget *label = label_new(title);
    gtk_widget_add_css_class(label, "heading");
    gtk_box_append(GTK_BOX(section), label);
  }
  gtk_widget_add_css_class(card, "card");
  gtk_widget_add_css_class(card, "profile-card");
  gtk_box_append(GTK_BOX(section), card);
  if (description) {
    GtkWidget *label = label_new(description);
    gtk_label_set_wrap(GTK_LABEL(label), TRUE);
    gtk_widget_add_css_class(label, "caption");
    gtk_widget_add_css_class(label, "dim-label");
    gtk_box_append(GTK_BOX(section), label);
  }
  gtk_box_append(GTK_BOX(content), section);
  return card;
}

static char *field_value(Field *field)
{
  if (field->kind == FIELD_TEXT)
    return g_strdup(gtk_editable_get_text(GTK_EDITABLE(field->widget)));
  if (field->kind == FIELD_SWITCH)
    return g_strdup(gtk_switch_get_active(GTK_SWITCH(field->widget)) ? "1" : "0");
  return g_strdup_printf("%u", gtk_drop_down_get_selected(GTK_DROP_DOWN(field->widget)));
}

static void field_free(Field *field)
{
  g_free(field->original);
  g_free(field);
}

static void sync_version(Editor *editor)
{
  /* Update2 can complete before libnm updates its local cache. Only adopt a
   * version whose settings match our snapshot, never an unrelated edit. */
  if (editor->profile && nm_connection_compare(NM_CONNECTION(editor->profile), editor->baseline,
                                               NM_SETTING_COMPARE_FLAG_IGNORE_SECRETS))
    editor->version = nm_remote_connection_get_version_id(editor->profile);
}

static void update_dirty(Editor *editor)
{
  guint count = 0;
  if (editor->building) return;
  for (guint i = 0; i < editor->fields->len; i++) {
    Field *field = g_ptr_array_index(editor->fields, i);
    g_autofree char *value = field_value(field);
    gboolean changed = g_strcmp0(value, field->original) != 0;
    gtk_widget_set_opacity(field->undo, changed ? 1 : 0);
    gtk_widget_set_sensitive(field->undo, changed);
    gtk_widget_set_can_target(field->undo, changed);
    gtk_widget_set_focusable(field->undo, changed);
    if (changed) gtk_widget_add_css_class(field->row, "modified");
    else gtk_widget_remove_css_class(field->row, "modified");
    count += changed;
  }
  if (!editor->dirty) sync_version(editor);
  editor->dirty = count != 0;
  g_autofree char *text = g_strdup_printf("%u changed %s", count, count == 1 ? "field" : "fields");
  gtk_label_set_text(GTK_LABEL(editor->change_count), text);
  gtk_widget_set_visible(editor->footer, editor->dirty || !editor->profile);
}

static void field_changed(GObject *object, GParamSpec *pspec, gpointer data)
{
  (void) object; (void) pspec;
  update_dirty(data);
}

static void field_revert(GtkButton *button, gpointer data)
{
  Field *field = data;
  (void) button;
  if (field->kind == FIELD_TEXT)
    gtk_editable_set_text(GTK_EDITABLE(field->widget), field->original);
  else if (field->kind == FIELD_SWITCH)
    gtk_switch_set_active(GTK_SWITCH(field->widget), g_str_equal(field->original, "1"));
  else gtk_drop_down_set_selected(GTK_DROP_DOWN(field->widget), (guint) g_ascii_strtoull(field->original, NULL, 10));
}

static void track(Editor *editor, GtkWidget *row, GtkWidget *widget, const char *title, FieldKind kind)
{
  Field *field = g_new0(Field, 1);
  field->widget = widget; field->row = row; field->kind = kind;
  field->original = field_value(field);
  field->undo = gtk_button_new_from_icon_name("edit-undo-symbolic");
  gtk_widget_add_css_class(field->undo, "flat");
  gtk_widget_add_css_class(field->undo, "field-undo");
  gtk_widget_set_opacity(field->undo, 0);
  gtk_widget_set_sensitive(field->undo, FALSE);
  gtk_widget_set_can_target(field->undo, FALSE);
  gtk_widget_set_focusable(field->undo, FALSE);
  g_autofree char *title_text = g_strdup_printf("Revert %s", title);
  gtk_widget_set_tooltip_text(field->undo, title_text);
  gtk_accessible_update_property(GTK_ACCESSIBLE(field->undo), GTK_ACCESSIBLE_PROPERTY_LABEL, title_text, -1);
  gtk_accessible_update_property(GTK_ACCESSIBLE(widget), GTK_ACCESSIBLE_PROPERTY_LABEL, title, -1);
  gtk_box_append(GTK_BOX(row), field->undo);
  g_ptr_array_add(editor->fields, field);
  g_signal_connect(field->undo, "clicked", G_CALLBACK(field_revert), field);
  g_signal_connect(widget, kind == FIELD_TEXT ? "notify::text" : kind == FIELD_SWITCH ? "notify::active" : "notify::selected",
                   G_CALLBACK(field_changed), editor);
}

static GtkWidget *entry(Editor *editor, GtkWidget *box, const char *title, const char *value, gboolean secret)
{
  GtkWidget *row = row_new(box, title);
  GtkWidget *widget = secret ? gtk_password_entry_new() : gtk_entry_new();
  if (secret) gtk_password_entry_set_show_peek_icon(GTK_PASSWORD_ENTRY(widget), TRUE);
  else gtk_entry_set_has_frame(GTK_ENTRY(widget), FALSE);
  gtk_editable_set_width_chars(GTK_EDITABLE(widget), 1);
  gtk_editable_set_text(GTK_EDITABLE(widget), value ? value : "");
  gtk_widget_set_hexpand(widget, TRUE);
  gtk_box_append(GTK_BOX(row), widget);
  track(editor, row, widget, title, FIELD_TEXT);
  return widget;
}

static GtkWidget *toggle(Editor *editor, GtkWidget *box, const char *title, gboolean active)
{
  GtkWidget *row = row_new(box, title);
  GtkWidget *widget = gtk_switch_new();
  gtk_widget_set_hexpand(widget, TRUE);
  gtk_widget_set_halign(widget, GTK_ALIGN_END);
  gtk_widget_set_valign(widget, GTK_ALIGN_CENTER);
  gtk_switch_set_active(GTK_SWITCH(widget), active);
  gtk_box_append(GTK_BOX(row), widget);
  track(editor, row, widget, title, FIELD_SWITCH);
  return widget;
}

static GtkWidget *combo(Editor *editor, GtkWidget *box, const char *title, const char *const *labels, guint selected)
{
  GtkWidget *row = row_new(box, title);
  GtkWidget *widget = gtk_drop_down_new_from_strings(labels);
  gtk_drop_down_set_selected(GTK_DROP_DOWN(widget), selected);
  gtk_widget_set_hexpand(widget, TRUE);
  gtk_box_append(GTK_BOX(row), widget);
  track(editor, row, widget, title, FIELD_CHOICE);
  return widget;
}

/* Diagnostics are selectable text, not controls. Updating their existing
 * labels preserves selection and avoids rebuilding the form on every scan. */
static void info(GtkWidget *box, const char *title, const char *value)
{
  GHashTable *rows = g_object_get_data(G_OBJECT(box), "info-rows");
  if (!rows) {
    rows = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    g_object_set_data_full(G_OBJECT(box), "info-rows", rows, (GDestroyNotify) g_hash_table_unref);
  }
  GtkWidget *label = g_hash_table_lookup(rows, title);
  if (!label) {
    GtkWidget *row = network_sidebar_compact_info_row(title, "", &label);
    gtk_box_append(GTK_BOX(box), row);
    g_hash_table_insert(rows, g_strdup(title), label);
  }
  const char *text = value && *value ? value : "—";
  if (g_strcmp0(gtk_label_get_text(GTK_LABEL(label)), text)) gtk_label_set_text(GTK_LABEL(label), text);
  gtk_widget_set_visible(gtk_widget_get_parent(label), value && *value);
}

static AdwNavigationPage *page_new(const char *title, GtkWidget **content, GtkWidget **header)
{
  GtkWidget *toolbar = adw_toolbar_view_new();
  GtkWidget *scroll = gtk_scrolled_window_new();
  *header = adw_header_bar_new();
  adw_header_bar_set_show_end_title_buttons(ADW_HEADER_BAR(*header), FALSE);
  adw_toolbar_view_add_top_bar(ADW_TOOLBAR_VIEW(toolbar), *header);
  *content = gtk_box_new(GTK_ORIENTATION_VERTICAL, 14);
  gtk_widget_add_css_class(*content, "profile-page");
  gtk_widget_set_margin_start(*content, 12);
  gtk_widget_set_margin_end(*content, 12);
  gtk_widget_set_margin_top(*content, 4);
  gtk_widget_set_margin_bottom(*content, 16);
  gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
  gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), *content);
  adw_toolbar_view_set_content(ADW_TOOLBAR_VIEW(toolbar), scroll);
  return adw_navigation_page_new(toolbar, title);
}

static void editor_free(Editor *editor)
{
  if (editor->refresh_timer) g_source_remove(editor->refresh_timer);
  network_sidebar_actions_unref(editor->actions);
  g_clear_object(&editor->client);
  g_clear_object(&editor->profile);
  g_clear_object(&editor->baseline);
  g_clear_object(&editor->submitted);
  g_clear_object(&editor->active);
  g_ptr_array_unref(editor->fields);
  g_free(editor->custom_mac);
  g_free(editor->device_mac);
  g_free(editor);
}

static void show_error(Editor *editor, const char *message)
{
  gtk_label_set_text(GTK_LABEL(editor->error), message ? message : "Could not save the connection.");
  gtk_widget_set_visible(editor->error, TRUE);
  gtk_widget_grab_focus(editor->error);
}

static void ip_method_changed(GObject *object, GParamSpec *pspec, gpointer data)
{
  IpFields *fields = data;
  guint selected = gtk_drop_down_get_selected(GTK_DROP_DOWN(fields->method));
  const char *method = fields->methods[selected];
  gboolean enabled = g_strcmp0(method, "disabled") && g_strcmp0(method, "ignore");
  (void) object; (void) pspec;
  gtk_widget_set_sensitive(fields->fields, enabled);
  gboolean automatic = gtk_switch_get_active(GTK_SWITCH(fields->automatic_dns));
  gboolean custom = *gtk_editable_get_text(GTK_EDITABLE(fields->dns)) != '\0';
  const char *dns = automatic ? custom ? "Auto + custom DNS" : "Auto DNS" : custom ? "Custom DNS" : "No DNS";
  g_autofree char *text = enabled ? g_strdup_printf("%s · %s", fields->labels[selected], dns) : g_strdup(fields->labels[selected]);
  gtk_label_set_text(GTK_LABEL(fields->summary), text);
}

static void ip_fields_new(Editor *editor, GtkWidget *content, const char *title, NMSettingIPConfig *setting, int family, IpFields *fields)
{
  const char *method = setting ? nm_setting_ip_config_get_method(setting) : "auto";
  guint selected = 0;
  g_autofree char *addresses = sidebar_profile_addresses(setting);
  g_autofree char *dns = sidebar_profile_dns(setting);
  GtkWidget *expander = gtk_expander_new(NULL);
  GtkWidget *heading = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 12);
  GtkWidget *label = label_new(title);
  fields->summary = label_new("");
  gtk_widget_set_hexpand(label, TRUE);
  gtk_widget_add_css_class(label, "heading");
  gtk_widget_add_css_class(fields->summary, "dim-label");
  gtk_box_append(GTK_BOX(heading), label);
  gtk_box_append(GTK_BOX(heading), fields->summary);
  gtk_expander_set_label_widget(GTK_EXPANDER(expander), heading);
  gtk_widget_add_css_class(expander, "profile-expander");
  GtkWidget *body = gtk_box_new(GTK_ORIENTATION_VERTICAL, 6);
  gtk_expander_set_child(GTK_EXPANDER(expander), body);
  gtk_box_append(GTK_BOX(content), expander);
  GtkWidget *settings_group = group(body, NULL, NULL);
  fields->methods = family == AF_INET ? methods4 : methods6;
  fields->labels = family == AF_INET ? labels4 : labels6;
  for (guint i = 0; fields->methods[i]; i++)
    if (g_strcmp0(method, fields->methods[i]) == 0) selected = i;
  fields->method = combo(editor, settings_group, "Method", fields->labels, selected);
  fields->fields = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
  gtk_box_append(GTK_BOX(settings_group), fields->fields);
  fields->addresses = entry(editor, fields->fields, "Addresses", addresses, FALSE);
  gtk_entry_set_placeholder_text(GTK_ENTRY(fields->addresses), family == AF_INET ? "192.168.1.20/24" : "2001:db8::20/64");
  gtk_widget_set_tooltip_text(fields->addresses, "Saved addresses with prefixes, separated by commas. DHCP-assigned addresses are shown below.");
  fields->gateway = entry(editor, fields->fields, "Gateway", setting ? nm_setting_ip_config_get_gateway(setting) : NULL, FALSE);
  gtk_entry_set_placeholder_text(GTK_ENTRY(fields->gateway), "Network default");
  fields->automatic_dns = toggle(editor, fields->fields, "Auto DNS", !setting || !nm_setting_ip_config_get_ignore_auto_dns(setting));
  fields->dns = entry(editor, fields->fields, "DNS servers", dns, FALSE);
  gtk_entry_set_placeholder_text(GTK_ENTRY(fields->dns), "Comma-separated IPs");
  gtk_widget_set_tooltip_text(fields->dns, "Adds to automatic DNS when enabled; replaces it when disabled.");
  g_signal_connect(fields->method, "notify::selected", G_CALLBACK(ip_method_changed), fields);
  g_signal_connect(fields->automatic_dns, "notify::active", G_CALLBACK(ip_method_changed), fields);
  g_signal_connect(fields->dns, "notify::text", G_CALLBACK(ip_method_changed), fields);
  ip_method_changed(NULL, NULL, fields);
  gtk_expander_set_expanded(GTK_EXPANDER(expander), g_strcmp0(method, "manual") == 0);
}

static gboolean apply_ip(Editor *editor, NMConnection *draft, int family, IpFields *fields, GError **error)
{
  (void) editor;
  return sidebar_profile_set_ip(draft, family,
    fields->methods[gtk_drop_down_get_selected(GTK_DROP_DOWN(fields->method))],
    gtk_editable_get_text(GTK_EDITABLE(fields->addresses)),
    gtk_editable_get_text(GTK_EDITABLE(fields->gateway)),
    gtk_editable_get_text(GTK_EDITABLE(fields->dns)),
    gtk_switch_get_active(GTK_SWITCH(fields->automatic_dns)), error);
}

static void security_changed(GObject *object, GParamSpec *pspec, gpointer data)
{
  Editor *editor = data;
  guint mode = gtk_drop_down_get_selected(GTK_DROP_DOWN(editor->security));
  (void) object; (void) pspec;
  gtk_widget_set_sensitive(editor->password, mode != 0);
  gtk_widget_set_visible(editor->eap_group, mode == 3);
}

static void save_finished(GObject *source, GAsyncResult *result, gpointer data)
{
  AdwNavigationPage *page = data;
  Editor *editor = g_object_get_data(G_OBJECT(page), "editor");
  g_autoptr(GError) error = NULL;
  (void) source;
  editor->saving = FALSE;
  gtk_widget_set_sensitive(editor->body, TRUE);
  gtk_widget_set_sensitive(editor->footer, TRUE);
  adw_navigation_page_set_can_pop(page, TRUE);
  if (network_sidebar_actions_save_profile_finish(result, &error)) {
    if (!editor->profile) adw_navigation_view_pop(editor->view);
    else {
      editor->building = TRUE;
      g_set_object(&editor->baseline, editor->submitted);
      nm_connection_clear_secrets(editor->baseline);
      if (editor->password) gtk_editable_set_text(GTK_EDITABLE(editor->password), "");
      /* Disabled IP methods deliberately clear stored overrides. Reflect that
       * in the controls as well, so a later edit cannot bring them back. */
      IpFields *ips[] = { &editor->ip4, &editor->ip6 };
      for (guint i = 0; i < 2; i++) {
        IpFields *ip = ips[i];
        if (!gtk_widget_get_sensitive(ip->fields)) {
          gtk_editable_set_text(GTK_EDITABLE(ip->addresses), "");
          gtk_editable_set_text(GTK_EDITABLE(ip->gateway), "");
          gtk_editable_set_text(GTK_EDITABLE(ip->dns), "");
        }
      }
      for (guint i = 0; i < editor->fields->len; i++) {
        Field *field = g_ptr_array_index(editor->fields, i);
        g_free(field->original);
        field->original = field_value(field);
      }
      editor->dirty = FALSE;
      editor->building = FALSE;
      update_dirty(editor);
      adw_navigation_page_set_title(page, gtk_editable_get_text(GTK_EDITABLE(editor->ssid)));
    }
  } else show_error(editor, error->message);
  g_clear_object(&editor->submitted);
  g_object_unref(page);
}

static void
save_clicked(GtkButton *button, gpointer user_data)
{
  Editor *editor = user_data;
  g_autoptr(NMConnection) draft = nm_simple_connection_new_clone(editor->baseline);
  g_autoptr(GError) error = NULL;
  NMSettingConnection *connection = nm_connection_get_setting_connection(draft);
  NMSettingWireless *wireless = nm_connection_get_setting_wireless(draft);
  NMSettingWirelessSecurity *security = nm_connection_get_setting_wireless_security(draft);
  NMSetting8021x *eap = nm_connection_get_setting_802_1x(draft);
  const char *name = gtk_editable_get_text(GTK_EDITABLE(editor->name));
  const char *ssid = gtk_editable_get_text(GTK_EDITABLE(editor->ssid));
  const char *password = editor->password ? gtk_editable_get_text(GTK_EDITABLE(editor->password)) : "";
  guint metered = gtk_drop_down_get_selected(GTK_DROP_DOWN(editor->metered));
  (void) button;

  if (!*name || !*ssid || strlen(ssid) > 32) {
    show_error(editor, "Enter a profile name and a Wi-Fi name of 1–32 bytes.");
    return;
  }
  g_object_set(connection, NM_SETTING_CONNECTION_ID, name,
               NM_SETTING_CONNECTION_AUTOCONNECT, gtk_switch_get_active(GTK_SWITCH(editor->autoconnect)),
               NM_SETTING_CONNECTION_METERED, metered == 0 ? NM_METERED_UNKNOWN : metered == 1 ? NM_METERED_YES : NM_METERED_NO, NULL);
  /* Preserve non-UTF-8 SSIDs unless the displayed field was actually edited. */
  g_autofree char *original_ssid = network_sidebar_ssid_text_from_bytes(nm_setting_wireless_get_ssid(wireless));
  if (g_strcmp0(ssid, original_ssid) != 0) {
    g_autoptr(GBytes) bytes = g_bytes_new(ssid, strlen(ssid));
    g_object_set(wireless, NM_SETTING_WIRELESS_SSID, bytes, NULL);
  }
  g_object_set(wireless, NM_SETTING_WIRELESS_HIDDEN, gtk_switch_get_active(GTK_SWITCH(editor->hidden)), NULL);
  guint mac = gtk_drop_down_get_selected(GTK_DROP_DOWN(editor->mac));
  const char *selected_mac = mac < G_N_ELEMENTS(mac_values) ? mac_values[mac] : editor->custom_mac;
#if USES_IWD
  if (mac == 1) {
    /* iwd supports a literal AddressOverride, but ignores "permanent". */
    if (!editor->device_mac) editor->device_mac = profile_device_mac(editor->client, draft);
    if (!editor->device_mac) {
      show_error(editor, "Could not identify a single Wi-Fi adapter for its device address.");
      return;
    }
    selected_mac = editor->device_mac;
  }
#endif
  /* Keep an inherited policy unset when saving other fields. A deliberate
   * change stores the selected policy as an explicit per-network override. */
  if (g_strcmp0(selected_mac, effective_mac_policy(wireless)) != 0)
    g_object_set(wireless, NM_SETTING_WIRELESS_CLONED_MAC_ADDRESS, selected_mac, NULL);
  if (editor->security) {
    guint mode = gtk_drop_down_get_selected(GTK_DROP_DOWN(editor->security));
    if (mode != 0) {
      NMSetting *setting = nm_setting_wireless_security_new();
      g_object_set(setting, NM_SETTING_WIRELESS_SECURITY_KEY_MGMT, mode == 3 ? "wpa-eap" : mode == 2 ? "sae" : "wpa-psk", NULL);
      nm_connection_add_setting(draft, setting);
      security = NM_SETTING_WIRELESS_SECURITY(setting);
      if (mode == 3) {
        NMSetting *auth = nm_setting_802_1x_new();
        eap = NM_SETTING_802_1X(auth);
        gboolean ttls = gtk_drop_down_get_selected(GTK_DROP_DOWN(editor->eap_method)) == 1;
        const char *ca = gtk_editable_get_text(GTK_EDITABLE(editor->ca));
        const char *domain = gtk_editable_get_text(GTK_EDITABLE(editor->domain));
        if (!*domain) { g_object_unref(auth); show_error(editor, "Enter the authentication server domain supplied by your network administrator."); return; }
        nm_setting_802_1x_add_eap_method(eap, ttls ? "ttls" : "peap");
        g_object_set(eap, NM_SETTING_802_1X_PHASE2_AUTH, ttls ? "pap" : "mschapv2",
                     NM_SETTING_802_1X_SYSTEM_CA_CERTS, !*ca, NULL);
        if (*ca && !nm_setting_802_1x_set_ca_cert(eap, ca, NM_SETTING_802_1X_CK_SCHEME_PATH, NULL, &error)) {
          g_object_unref(auth); show_error(editor, error->message); return;
        }
        nm_connection_add_setting(draft, auth);
      }
      if (!*password) { show_error(editor, "Enter the Wi-Fi password."); return; }
    } else security = NULL;
  }
  if (security && *password) {
    const char *key = nm_setting_wireless_security_get_key_mgmt(security);
    if (g_strcmp0(key, "wpa-psk") == 0 || g_strcmp0(key, "sae") == 0) {
      if ((g_strcmp0(key, "wpa-psk") == 0 && !nm_utils_wpa_psk_valid(password)) ||
          (g_strcmp0(key, "sae") == 0 && strlen(password) > 63)) {
        show_error(editor, "Use a valid Wi-Fi password (WPA2: 8–63 characters or 64 hexadecimal digits).");
        return;
      }
      g_object_set(security, NM_SETTING_WIRELESS_SECURITY_PSK, password,
                   NM_SETTING_WIRELESS_SECURITY_PSK_FLAGS, NM_SETTING_SECRET_FLAG_NONE, NULL);
    }
  }
  if (eap && editor->identity) {
    const char *identity = gtk_editable_get_text(GTK_EDITABLE(editor->identity));
    const char *anonymous = gtk_editable_get_text(GTK_EDITABLE(editor->anonymous));
    const char *domain = gtk_editable_get_text(GTK_EDITABLE(editor->domain));
    g_object_set(eap, NM_SETTING_802_1X_IDENTITY, *identity ? identity : NULL,
                 NM_SETTING_802_1X_ANONYMOUS_IDENTITY, *anonymous ? anonymous : NULL,
                 NM_SETTING_802_1X_DOMAIN_SUFFIX_MATCH, *domain ? domain : NULL, NULL);
    if (*password) g_object_set(eap, NM_SETTING_802_1X_PASSWORD, password,
                               NM_SETTING_802_1X_PASSWORD_FLAGS, NM_SETTING_SECRET_FLAG_NONE, NULL);
  }
  if (!apply_ip(editor, draft, AF_INET, &editor->ip4, &error) ||
      !apply_ip(editor, draft, AF_INET6, &editor->ip6, &error) ||
      /* Use the same canonical form as NM, including its legacy MAC
       * randomization property, before recording the saved baseline. */
      !nm_connection_normalize(draft, NULL, NULL, &error)) {
    show_error(editor, error->message);
    return;
  }
  g_set_object(&editor->submitted, draft);
  editor->saving = TRUE;
  gtk_widget_set_visible(editor->error, FALSE);
  gtk_widget_set_sensitive(editor->body, FALSE);
  gtk_widget_set_sensitive(editor->footer, FALSE);
  adw_navigation_page_set_can_pop(editor->page, FALSE);
  network_sidebar_actions_save_profile(editor->actions, editor->profile, draft, editor->version,
                                      save_finished, g_object_ref(editor->page));
}


static void revert_all(GtkButton *button, gpointer data)
{
  Editor *editor = data;
  (void) button;
  editor->building = TRUE;
  for (guint i = 0; i < editor->fields->len; i++) field_revert(NULL, g_ptr_array_index(editor->fields, i));
  editor->building = FALSE;
  gtk_widget_set_visible(editor->error, FALSE);
  update_dirty(editor);
}

static void connection_clicked(GtkButton *button, gpointer data)
{
  Editor *editor = data;
  (void) button;
  if (editor->active && nm_active_connection_get_state(editor->active) < NM_ACTIVE_CONNECTION_STATE_DEACTIVATING)
    network_sidebar_actions_deactivate(editor->actions, editor->active);
  else if (editor->profile)
    network_sidebar_actions_activate_saved_wifi_profile(editor->actions, editor->profile);
}

static void forget_confirmed(GtkButton *button, gpointer data)
{
  Editor *editor = data;
  (void) button;
  network_sidebar_actions_delete_connection(editor->actions, editor->profile);
  adw_navigation_view_pop(editor->view);
  adw_navigation_view_pop(editor->view);
}

static void forget_clicked(GtkButton *button, gpointer data)
{
  Editor *editor = data;
  GtkWidget *content, *header;
  AdwNavigationPage *page = page_new("Forget network?", &content, &header);
  GtkWidget *notice = label_new("This removes the saved Wi-Fi profile and password. If connected, you will be disconnected.");
  GtkWidget *confirm = gtk_button_new_with_label("Forget network");
  (void) button;
  gtk_label_set_wrap(GTK_LABEL(notice), TRUE);
  gtk_widget_add_css_class(confirm, "destructive-action");
  gtk_box_append(GTK_BOX(content), notice);
  gtk_box_append(GTK_BOX(content), confirm);
  g_signal_connect(confirm, "clicked", G_CALLBACK(forget_confirmed), editor);
  adw_navigation_view_push(editor->view, page);
}

static char *live_addresses(NMIPConfig *config)
{
  GString *text = g_string_new(NULL);
  GPtrArray *addresses = config ? nm_ip_config_get_addresses(config) : NULL;
  for (guint i = 0; addresses && i < addresses->len; i++) {
    NMIPAddress *address = g_ptr_array_index(addresses, i);
    if (i) g_string_append_c(text, '\n');
    g_string_append_printf(text, "%s/%u", nm_ip_address_get_address(address), nm_ip_address_get_prefix(address));
  }
  return g_string_free(text, FALSE);
}

static char *live_routes(NMIPConfig *config)
{
  GString *text = g_string_new(NULL);
  GPtrArray *routes = config ? nm_ip_config_get_routes(config) : NULL;
  for (guint i = 0; routes && i < routes->len; i++) {
    NMIPRoute *route = g_ptr_array_index(routes, i);
    const char *hop = nm_ip_route_get_next_hop(route);
    if (i) g_string_append_c(text, '\n');
    g_string_append_printf(text, "%s/%u", nm_ip_route_get_dest(route), nm_ip_route_get_prefix(route));
    if (hop && *hop) g_string_append_printf(text, " via %s", hop);
    if (nm_ip_route_get_metric(route) >= 0)
      g_string_append_printf(text, " · metric %" G_GINT64_FORMAT, nm_ip_route_get_metric(route));
  }
  return g_string_free(text, FALSE);
}

static void live_ip(Editor *editor, NMIPConfig *ip, const char *family)
{
  g_autofree char *addresses = live_addresses(ip);
  g_autofree char *dns = ip ? g_strjoinv(", ", (char **) nm_ip_config_get_nameservers(ip)) : NULL;
  g_autofree char *routes = live_routes(ip);
  g_autofree char *domains = ip ? g_strjoinv(", ", (char **) nm_ip_config_get_domains(ip)) : NULL;
  g_autofree char *searches = ip ? g_strjoinv(", ", (char **) nm_ip_config_get_searches(ip)) : NULL;
  g_autofree char *gateway_key = g_strdup_printf("%s gateway", family);
  g_autofree char *dns_key = g_strdup_printf("%s DNS", family);
  g_autofree char *routes_key = g_strdup_printf("%s routes", family);
  g_autofree char *domains_key = g_strdup_printf("%s domains", family);
  g_autofree char *searches_key = g_strdup_printf("%s searches", family);
  info(editor->live, family, addresses);
  info(editor->live, gateway_key, ip ? nm_ip_config_get_gateway(ip) : NULL);
  info(editor->live, dns_key, dns);
  info(editor->diagnostics, routes_key, routes);
  info(editor->diagnostics, domains_key, domains);
  info(editor->diagnostics, searches_key, searches);
}

static const char *security_label(NMSettingWirelessSecurity *security)
{
  const char *key = security ? nm_setting_wireless_security_get_key_mgmt(security) : NULL;
  if (!key) return "Open network";
  if (g_str_equal(key, "wpa-psk")) return "WPA Personal";
  if (g_str_equal(key, "sae")) return "WPA3 Personal";
  if (g_str_equal(key, "wpa-eap")) return "WPA Enterprise";
  if (g_str_equal(key, "wpa-eap-suite-b-192")) return "WPA3 Enterprise";
  if (g_str_equal(key, "owe")) return "Enhanced Open (OWE)";
  return key;
}

static gboolean refresh_live(gpointer data)
{
  Editor *editor = data;
  if (adw_navigation_view_get_visible_page(editor->view) != editor->page ||
      !gtk_widget_get_mapped(GTK_WIDGET(editor->page))) return G_SOURCE_CONTINUE;
  const GPtrArray *connections = nm_client_get_active_connections(editor->client);
  g_clear_object(&editor->active);
  for (guint i = 0; connections && i < connections->len; i++) {
    NMActiveConnection *active = g_ptr_array_index(connections, i);
    if (g_strcmp0(nm_active_connection_get_uuid(active), nm_connection_get_uuid(editor->baseline)) == 0)
      editor->active = g_object_ref(active);
  }
  NMDevice *device = NULL;
  NMAccessPoint *ap = NULL;
  const GPtrArray *devices = editor->active ? nm_active_connection_get_devices(editor->active) : NULL;
  if (devices && devices->len) device = g_ptr_array_index(devices, 0);
  if (NM_IS_DEVICE_WIFI(device)) ap = nm_device_wifi_get_active_access_point(NM_DEVICE_WIFI(device));
  gboolean connected = editor->active && nm_active_connection_get_state(editor->active) == NM_ACTIVE_CONNECTION_STATE_ACTIVATED;
  g_autofree char *state = editor->active ? network_sidebar_active_state_label(nm_active_connection_get_state(editor->active)) : g_strdup("Disconnected");
  g_autofree char *frequency = ap ? network_sidebar_frequency_label(nm_access_point_get_frequency(ap)) : NULL;
  g_autofree char *status = connected && ap ? g_strdup_printf("Connected · %u%% signal · %s", nm_access_point_get_strength(ap), frequency) : g_strdup(state);
  gtk_label_set_text(GTK_LABEL(editor->status), status);
  if (connected) gtk_widget_add_css_class(editor->status, "success");
  else gtk_widget_remove_css_class(editor->status, "success");
  gtk_button_set_label(GTK_BUTTON(editor->connect), editor->active ? "Disconnect" : "Connect");
  gtk_widget_set_visible(gtk_widget_get_parent(editor->live), editor->active != NULL);
  info(editor->diagnostics, "Interface", device ? nm_device_get_iface(device) : NULL);
  info(editor->diagnostics, "Device state", device ? nm_device_get_state(device) == NM_DEVICE_STATE_ACTIVATED ? "Connected" : state : NULL);
  info(editor->diagnostics, "MAC address", device ? nm_device_get_hw_address(device) : NULL);
  info(editor->diagnostics, "Driver", device ? nm_device_get_driver(device) : NULL);
  info(editor->diagnostics, "BSSID", ap ? nm_access_point_get_bssid(ap) : NULL);
  g_autofree char *radio = ap ? g_strdup_printf("%u MHz · channel %u", nm_access_point_get_frequency(ap),
    nm_utils_wifi_freq_to_channel(nm_access_point_get_frequency(ap))) : NULL;
  info(editor->diagnostics, "Radio", radio);
  g_autofree char *security = ap ? network_sidebar_ap_security_label(ap) : NULL;
  info(editor->diagnostics, "AP security", security);
  info(editor->diagnostics, "Default route", editor->active ?
       nm_active_connection_get_default(editor->active) ? nm_active_connection_get_default6(editor->active) ? "IPv4 + IPv6" : "IPv4" :
       nm_active_connection_get_default6(editor->active) ? "IPv6" : "No" : NULL);
  live_ip(editor, editor->active ? nm_active_connection_get_ip4_config(editor->active) : NULL, "IPv4");
  live_ip(editor, editor->active ? nm_active_connection_get_ip6_config(editor->active) : NULL, "IPv6");
  if (!editor->dirty && !editor->saving) sync_version(editor);
  return G_SOURCE_CONTINUE;
}

void
network_sidebar_edit_profile(NetworkSidebarActions *actions, NMClient *client,
                             AdwNavigationView *view, NMRemoteConnection *profile,
                             const char *initial_ssid, gboolean enterprise)
{
  Editor *editor = g_new0(Editor, 1);
  GtkWidget *header, *settings_group;
  const char *const meter_labels[] = { "Automatic", "Metered", "Unmetered", NULL };
  const char *const security_labels[] = { "Open network", "WPA2 Personal", "WPA3 Personal", "Enterprise (802.1X)", NULL };
  const char *const eap_labels[] = { "PEAP (MSCHAPv2)", "TTLS (PAP)", NULL };
#if USES_IWD
  const char *mac_labels[] = { "Stable per network", "Device address", "Random", "Custom address", NULL };
#else
  const char *mac_labels[] = { "Stable per network", "Device address", "Random", "Keep current", "Stable per profile", "Custom address", NULL };
#endif

  editor->building = TRUE;
  editor->fields = g_ptr_array_new_with_free_func((GDestroyNotify) field_free);
  editor->actions = network_sidebar_actions_ref(actions);
  editor->client = g_object_ref(client);
  editor->view = view;
  if (profile) {
    editor->profile = g_object_ref(profile);
    editor->baseline = nm_simple_connection_new_clone(NM_CONNECTION(profile));
    editor->version = nm_remote_connection_get_version_id(profile);
  } else {
    g_autofree char *uuid = g_uuid_string_random();
    NMSetting *setting = nm_setting_connection_new();
    editor->baseline = nm_simple_connection_new();
    g_object_set(setting, NM_SETTING_CONNECTION_UUID, uuid, NM_SETTING_CONNECTION_ID, initial_ssid ? initial_ssid : "New Wi-Fi",
                 NM_SETTING_CONNECTION_TYPE, NM_SETTING_WIRELESS_SETTING_NAME, NM_SETTING_CONNECTION_AUTOCONNECT, FALSE, NULL);
    nm_connection_add_setting(editor->baseline, setting);
    setting = nm_setting_wireless_new();
    g_autoptr(GBytes) bytes = g_bytes_new(initial_ssid ? initial_ssid : "", initial_ssid ? strlen(initial_ssid) : 0);
    g_object_set(setting, NM_SETTING_WIRELESS_MODE, "infrastructure", NM_SETTING_WIRELESS_SSID, bytes, NULL);
    nm_connection_add_setting(editor->baseline, setting);
  }
  NMSettingConnection *connection = nm_connection_get_setting_connection(editor->baseline);
  NMSettingWireless *wireless = nm_connection_get_setting_wireless(editor->baseline);
  NMSettingWirelessSecurity *security = nm_connection_get_setting_wireless_security(editor->baseline);
  NMSetting8021x *eap = nm_connection_get_setting_802_1x(editor->baseline);
  if (!wireless) { editor_free(editor); return; }
  g_autofree char *ssid = profile ? network_sidebar_ssid_text_from_bytes(nm_setting_wireless_get_ssid(wireless)) : g_strdup(initial_ssid ? initial_ssid : "");
  editor->page = page_new(profile ? ssid : "Add Wi-Fi", &editor->body, &header);
  g_object_set_data_full(G_OBJECT(editor->page), "editor", editor, (GDestroyNotify) editor_free);

  editor->status = label_new(profile ? "Saved network" : "New network");
  gtk_widget_add_css_class(editor->status, "caption");
  gtk_box_append(GTK_BOX(editor->body), editor->status);
  GtkWidget *controls = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
  editor->connect = gtk_button_new_with_label("Connect");
  gtk_widget_set_visible(editor->connect, profile != NULL);
  g_signal_connect(editor->connect, "clicked", G_CALLBACK(connection_clicked), editor);
  gtk_box_append(GTK_BOX(controls), editor->connect);
  GtkWidget *auto_label = label_new("Auto-connect");
  gtk_widget_set_hexpand(auto_label, TRUE);
  gtk_label_set_xalign(GTK_LABEL(auto_label), 1);
  gtk_box_append(GTK_BOX(controls), auto_label);
  editor->autoconnect = gtk_switch_new();
  gtk_widget_set_valign(editor->autoconnect, GTK_ALIGN_CENTER);
  gtk_switch_set_active(GTK_SWITCH(editor->autoconnect), nm_setting_connection_get_autoconnect(connection));
  gtk_box_append(GTK_BOX(controls), editor->autoconnect);
  track(editor, controls, editor->autoconnect, "Auto-connect", FIELD_SWITCH);
  gtk_box_append(GTK_BOX(editor->body), controls);

  editor->error = label_new("");
  gtk_label_set_wrap(GTK_LABEL(editor->error), TRUE);
  gtk_widget_set_focusable(editor->error, TRUE);
  gtk_widget_add_css_class(editor->error, "error");
  gtk_widget_set_visible(editor->error, FALSE);
  gtk_box_append(GTK_BOX(editor->body), editor->error);
  settings_group = group(editor->body, "Saved configuration", profile ? "Changes apply next time you connect. Back discards unsaved edits." : NULL);
  editor->ssid = entry(editor, settings_group, "Wi-Fi name", ssid, FALSE);
  editor->name = entry(editor, settings_group, "Profile name", nm_setting_connection_get_id(connection), FALSE);
  if (!profile) editor->security = combo(editor, settings_group, "Security", security_labels, enterprise ? 3 : 1);
  if (!profile || (security && (g_strcmp0(nm_setting_wireless_security_get_key_mgmt(security), "wpa-psk") == 0 ||
                               g_strcmp0(nm_setting_wireless_security_get_key_mgmt(security), "sae") == 0)) || eap) {
    editor->password = entry(editor, settings_group, "Password", NULL, TRUE);
    g_object_set(editor->password, "placeholder-text", profile ? "Unchanged" : "Required for secured Wi-Fi", NULL);
    gtk_widget_set_tooltip_text(editor->password, "Enter a replacement, or leave blank to keep the saved password.");
  }
  NMMetered metered = nm_setting_connection_get_metered(connection);
  editor->metered = combo(editor, settings_group, "Data usage", meter_labels, metered == NM_METERED_YES ? 1 : metered == NM_METERED_NO ? 2 : 0);
  editor->hidden = toggle(editor, settings_group, "Hidden SSID", nm_setting_wireless_get_hidden(wireless));
  gtk_widget_set_tooltip_text(editor->hidden, "Enable only if this network does not broadcast its name.");
  const char *mac = effective_mac_policy(wireless);
  editor->device_mac = profile_device_mac(client, editor->baseline);
  guint mac_index = G_N_ELEMENTS(mac_values);
  for (guint i = 0; i < G_N_ELEMENTS(mac_values); i++) if (g_strcmp0(mac, mac_values[i]) == 0) mac_index = i;
#if USES_IWD
  if (editor->device_mac && g_ascii_strcasecmp(mac, editor->device_mac) == 0) mac_index = 1;
#endif
  if (mac_index < G_N_ELEMENTS(mac_values)) mac_labels[G_N_ELEMENTS(mac_values)] = NULL;
  else editor->custom_mac = g_strdup(mac);
  editor->mac = combo(editor, settings_group, "MAC policy", mac_labels, mac_index);
  if (eap || !profile) {
    editor->eap_group = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_box_append(GTK_BOX(editor->body), editor->eap_group);
    settings_group = group(editor->eap_group, "Authentication", eap ? "Existing EAP methods and certificates are retained." : "Use the domain and certificate supplied by your network administrator.");
    if (!profile) editor->eap_method = combo(editor, settings_group, "Method", eap_labels, 0);
    editor->identity = entry(editor, settings_group, "Identity", eap ? nm_setting_802_1x_get_identity(eap) : NULL, FALSE);
    editor->anonymous = entry(editor, settings_group, "Anonymous ID", eap ? nm_setting_802_1x_get_anonymous_identity(eap) : NULL, FALSE);
    editor->domain = entry(editor, settings_group, "Server domain", eap ? nm_setting_802_1x_get_domain_suffix_match(eap) : NULL, FALSE);
    if (!profile) {
      editor->ca = entry(editor, settings_group, "CA certificate", NULL, FALSE);
      g_signal_connect(editor->security, "notify::selected", G_CALLBACK(security_changed), editor);
      security_changed(NULL, NULL, editor);
    }
  }
  ip_fields_new(editor, editor->body, "IPv4", nm_connection_get_setting_ip4_config(editor->baseline), AF_INET, &editor->ip4);
  ip_fields_new(editor, editor->body, "IPv6", nm_connection_get_setting_ip6_config(editor->baseline), AF_INET6, &editor->ip6);

  if (profile) {
    editor->live = group(editor->body, "Live connection", "Assigned by the network; saved overrides are above.");
    GtkWidget *expander = gtk_expander_new("Device & profile details");
    gtk_widget_add_css_class(expander, "profile-expander");
    GtkWidget *details = gtk_box_new(GTK_ORIENTATION_VERTICAL, 6);
    gtk_expander_set_child(GTK_EXPANDER(expander), details);
    gtk_box_append(GTK_BOX(editor->body), expander);
    editor->diagnostics = group(details, NULL, NULL);
    info(editor->diagnostics, "Security", security_label(security));
    info(editor->diagnostics, "UUID", nm_connection_get_uuid(editor->baseline));
    if (mac_index == G_N_ELEMENTS(mac_values)) info(editor->diagnostics, "Custom option", mac);
    info(editor->diagnostics, "BSSID lock", nm_setting_wireless_get_bssid(wireless));
    info(editor->diagnostics, "Band lock", nm_setting_wireless_get_band(wireless));
    if (eap) {
      GString *methods = g_string_new(NULL);
      for (guint i = 0; i < nm_setting_802_1x_get_num_eap_methods(eap); i++) {
        if (i) g_string_append(methods, ", ");
        g_string_append(methods, nm_setting_802_1x_get_eap_method(eap, i));
      }
      info(editor->diagnostics, "EAP method", methods->str);
      g_string_free(methods, TRUE);
      info(editor->diagnostics, "Inner auth", nm_setting_802_1x_get_phase2_auth(eap));
      NMSetting8021xCKScheme scheme = nm_setting_802_1x_get_ca_cert_scheme(eap);
      info(editor->diagnostics, "CA certificate", scheme == NM_SETTING_802_1X_CK_SCHEME_PATH ? nm_setting_802_1x_get_ca_cert_path(eap) :
        scheme == NM_SETTING_802_1X_CK_SCHEME_BLOB ? "Embedded certificate" :
        scheme == NM_SETTING_802_1X_CK_SCHEME_PKCS11 ? "PKCS#11 certificate" : nm_setting_802_1x_get_system_ca_certs(eap) ? "System certificates" : "Not configured");
    }
    GtkWidget *forget = gtk_button_new_with_label("Forget network…");
    gtk_widget_add_css_class(forget, "flat");
    gtk_widget_add_css_class(forget, "error");
    gtk_widget_set_halign(forget, GTK_ALIGN_START);
    g_signal_connect(forget, "clicked", G_CALLBACK(forget_clicked), editor);
    gtk_box_append(GTK_BOX(editor->body), forget);
    editor->refresh_timer = g_timeout_add_seconds(2, refresh_live, editor);
  }

  editor->footer = gtk_box_new(GTK_ORIENTATION_VERTICAL, 6);
  gtk_widget_add_css_class(editor->footer, "profile-save-bar");
  editor->change_count = label_new("");
  gtk_widget_add_css_class(editor->change_count, "caption");
  gtk_box_append(GTK_BOX(editor->footer), editor->change_count);
  GtkWidget *buttons = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
  GtkWidget *revert = gtk_button_new_with_label("Revert all");
  g_signal_connect(revert, "clicked", G_CALLBACK(revert_all), editor);
  gtk_box_append(GTK_BOX(buttons), revert);
  editor->save = gtk_button_new_with_label(profile ? "Save changes" : "Save network");
  gtk_widget_set_hexpand(editor->save, TRUE);
  gtk_widget_add_css_class(editor->save, "suggested-action");
  g_signal_connect(editor->save, "clicked", G_CALLBACK(save_clicked), editor);
  gtk_box_append(GTK_BOX(buttons), editor->save);
  gtk_box_append(GTK_BOX(editor->footer), buttons);
  adw_toolbar_view_add_bottom_bar(ADW_TOOLBAR_VIEW(adw_navigation_page_get_child(editor->page)), editor->footer);
  editor->building = FALSE;
  update_dirty(editor);
  adw_navigation_view_push(view, editor->page);
  if (profile) refresh_live(editor);
  /* Do not select the SSID on entry: this is a details view until edited. */
  gtk_widget_set_focusable(editor->status, TRUE);
  gtk_widget_grab_focus(editor->status);
}

void
network_sidebar_show_profile(NetworkSidebarActions *actions, NMClient *client,
                             AdwNavigationView *view, NMRemoteConnection *profile,
                             NMActiveConnection *active)
{
  if (profile && nm_connection_get_setting_wireless(NM_CONNECTION(profile))) {
    network_sidebar_edit_profile(actions, client, view, profile, NULL, FALSE);
    return;
  }
  /* An active connection can briefly outlive its saved profile. */
  GtkWidget *content, *header;
  AdwNavigationPage *page = page_new("Connection details", &content, &header);
  network_sidebar_add_active_connection_info_content(GTK_BOX(content), client, active);
  adw_navigation_view_push(view, page);
}
