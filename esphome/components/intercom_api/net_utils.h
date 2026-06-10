#pragma once

#include <cstdint>
#include <cerrno>
#include <sys/select.h>

namespace esphome {
namespace intercom_api {

static inline timeval make_timeval_ms(uint32_t timeout_ms) {
  timeval tv{};
  tv.tv_sec = timeout_ms / 1000;
  tv.tv_usec = (timeout_ms % 1000) * 1000;
  return tv;
}

static inline bool wait_socket_readable(int socket, uint32_t timeout_ms) {
  if (socket < 0) return false;
  fd_set fds;
  FD_ZERO(&fds);
  FD_SET(socket, &fds);
  timeval tv = make_timeval_ms(timeout_ms);
  int ret = select(socket + 1, &fds, nullptr, nullptr, &tv);
  return ret > 0 && FD_ISSET(socket, &fds);
}

static inline bool wait_socket_writable(int socket, uint32_t timeout_ms) {
  if (socket < 0) return false;
  fd_set fds;
  FD_ZERO(&fds);
  FD_SET(socket, &fds);
  timeval tv = make_timeval_ms(timeout_ms);
  int ret = select(socket + 1, nullptr, &fds, nullptr, &tv);
  return ret > 0 && FD_ISSET(socket, &fds);
}

}  // namespace intercom_api
}  // namespace esphome
