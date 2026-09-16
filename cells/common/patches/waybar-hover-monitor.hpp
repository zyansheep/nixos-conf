#pragma once
#include <gtkmm.h>
#include <gtk-layer-shell.h>
#include <algorithm>
#include <json/json.h>
#include <fstream>
#include <ctime>
#include <vector>

namespace waybar {
// No grab: the pointer may move from the bar into the scrollable panel, and
// background applications continue to receive their own pointer events.
class HoverMonitor : public sigc::trackable {
  Gtk::Window popup_;
  Gtk::Widget& anchor_;
  Gtk::Box body_{Gtk::ORIENTATION_VERTICAL, 8};
  Gtk::Label title_, note_;
  Gtk::ScrolledWindow scroll_;
  Gtk::Box rows_{Gtk::ORIENTATION_VERTICAL, 4};
  struct Row { Gtk::Box* box; Gtk::Label* name; Gtk::Label* value; };
  std::vector<Row> row_widgets_;
  std::string kind_;
  sigc::connection leave_, refresh_;
  bool inside_ = false;

  void show() {
    update();
    auto* top = anchor_.get_toplevel();
    auto display = anchor_.get_display();
    auto monitor = display->get_monitor_at_window(top->get_window());
    gtk_layer_set_monitor(popup_.gobj(), monitor->gobj());
    Gdk::Rectangle geometry;
    monitor->get_geometry(geometry);
    int x = 0, y = 0;
    anchor_.translate_coordinates(*top, 0, 0, x, y);
    Gtk::Requisition minimum, natural;
    body_.get_preferred_size(minimum, natural);
    const int left = std::clamp(x + anchor_.get_allocated_width() / 2 - natural.width / 2,
                                0, std::max(0, geometry.get_width() - natural.width));
    gtk_layer_set_margin(popup_.gobj(), GTK_LAYER_SHELL_EDGE_LEFT, left);
    gtk_layer_set_margin(popup_.gobj(), GTK_LAYER_SHELL_EDGE_TOP,
                         y + anchor_.get_allocated_height() + 4);
    popup_.resize(natural.width, natural.height);
    popup_.show();
  }
  void cancelClose() { leave_.disconnect(); }
  void scheduleClose() {
    cancelClose();
    leave_ = Glib::signal_timeout().connect([this] {
      if (!inside_) popup_.hide();
      return false;
    }, 180);
  }
  void update() {
    Json::Value state;
    std::ifstream input(std::string(g_get_user_runtime_dir()) + "/waybar-monitor/snapshot.json");
    Json::CharReaderBuilder builder;
    std::string errors;
    bool valid = Json::parseFromStream(builder, input, &state, &errors) &&
                 std::abs(std::time(nullptr) - state["timestamp"].asDouble()) < 10;
    if (!valid) {
      note_.set_text("Waiting for the background monitor…");
    } else {
      note_.set_text(kind_ == "cpu"
        ? "Top 100 · last " + std::to_string(state["coverage"].asInt()) + "s · % of whole CPU"
        : kind_ == "memory" ? "Top 100 · current RSS · shared pages counted per process"
        : "All readable hwmon and thermal-zone sensors");
    }
    const auto& readings = state[kind_];
    const size_t count = valid ? readings.size() : 0;
    // Reuse rows so a refresh does not tear down the viewport and reset scrolling.
    while (row_widgets_.size() < std::max(size_t{1}, count)) {
      auto* line = Gtk::manage(new Gtk::Box(Gtk::ORIENTATION_HORIZONTAL, 12));
      auto* name = Gtk::manage(new Gtk::Label());
      name->set_xalign(0);
      name->set_ellipsize(Pango::ELLIPSIZE_END);
      name->set_max_width_chars(32);
      auto* value = Gtk::manage(new Gtk::Label());
      value->get_style_context()->add_class("monitor-value");
      line->pack_start(*name, true, true);
      line->pack_end(*value, false, false);
      rows_.pack_start(*line, false, false);
      line->show_all();
      row_widgets_.push_back({line, name, value});
    }
    for (size_t i = 0; i < row_widgets_.size(); ++i) {
      auto& row = row_widgets_[i];
      row.box->set_visible(i < std::max(size_t{1}, count));
      if (i < count) {
        row.name->set_text(readings[static_cast<Json::ArrayIndex>(i)]["name"].asString());
        row.value->set_text(readings[static_cast<Json::ArrayIndex>(i)]["value"].asString());
      } else if (i == 0) {
        row.name->set_text(!valid ? "" : kind_ == "cpu"
          ? "No CPU activity recorded yet…" : "No readings available");
        row.value->set_text("");
      }
    }

  }

 public:
  HoverMonitor(Gtk::Widget& anchor, const std::string& kind) : anchor_(anchor), kind_(kind) {
    popup_.set_name("system-monitor-menu");
    popup_.set_modal(false);
    popup_.set_decorated(false);
    popup_.set_resizable(false);
    gtk_layer_init_for_window(popup_.gobj());
    gtk_layer_set_namespace(popup_.gobj(), "waybar-monitor");
    gtk_layer_set_layer(popup_.gobj(), GTK_LAYER_SHELL_LAYER_OVERLAY);
    gtk_layer_set_keyboard_mode(popup_.gobj(), GTK_LAYER_SHELL_KEYBOARD_MODE_NONE);
    gtk_layer_set_exclusive_zone(popup_.gobj(), -1);
    gtk_layer_set_anchor(popup_.gobj(), GTK_LAYER_SHELL_EDGE_TOP, true);
    gtk_layer_set_anchor(popup_.gobj(), GTK_LAYER_SHELL_EDGE_LEFT, true);
    body_.set_border_width(12);
    body_.set_size_request(380, -1);
    title_.set_text(kind == "cpu" ? "CPU · past minute" : kind == "memory" ? "Memory · programs" : "Temperatures");
    title_.set_xalign(0);
    title_.get_style_context()->add_class("monitor-title");
    note_.set_xalign(0);
    note_.set_line_wrap(true);
    note_.set_max_width_chars(48);
    note_.get_style_context()->add_class("monitor-note");
    scroll_.set_policy(Gtk::POLICY_NEVER, Gtk::POLICY_AUTOMATIC);
    scroll_.set_overlay_scrolling(false);
    scroll_.set_propagate_natural_height(true);
    scroll_.set_max_content_height(440);
    scroll_.add(rows_);
    body_.pack_start(title_, false, false);
    body_.pack_start(note_, false, false);
    body_.pack_start(scroll_, true, true);
    popup_.add(body_);
    body_.show_all();
    anchor.add_events(Gdk::ENTER_NOTIFY_MASK | Gdk::LEAVE_NOTIFY_MASK);
    anchor.signal_enter_notify_event().connect(sigc::track_obj([this](GdkEventCrossing*) {
      cancelClose(); show(); return false;
    }, *this));
    anchor.signal_leave_notify_event().connect(sigc::track_obj([this](GdkEventCrossing* event) {
      if (event->detail != GDK_NOTIFY_INFERIOR) scheduleClose();
      return false;
    }, *this));
    popup_.add_events(Gdk::ENTER_NOTIFY_MASK | Gdk::LEAVE_NOTIFY_MASK);
    popup_.signal_enter_notify_event().connect([this](GdkEventCrossing*) {
      inside_ = true; cancelClose(); return false;
    });
    popup_.signal_leave_notify_event().connect([this](GdkEventCrossing* event) {
      if (event->detail != GDK_NOTIFY_INFERIOR) { inside_ = false; scheduleClose(); }
      return false;
    });
    refresh_ = Glib::signal_timeout().connect_seconds([this] {
      if (popup_.get_visible()) update();
      return true;
    }, 2);
  }
  ~HoverMonitor() { leave_.disconnect(); refresh_.disconnect(); }
};
}  // namespace waybar
