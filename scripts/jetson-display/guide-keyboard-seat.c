/* Keep the operator seat keyboard-capable on touch-only hardware.
 * No input reports/key presses are ever generated. Closing the fd destroys
 * the device. UHID ABI: https://docs.kernel.org/hid/uhid.html
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/uhid.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;
static void stop(int signum) { (void)signum; stopping = 1; }
static int send_event(int fd, const struct uhid_event *event)
{
    ssize_t n;
    do { n = write(fd, event, sizeof(*event)); } while (n < 0 && errno == EINTR);
    if (n == sizeof(*event)) return 0;
    perror("UHID write");
    return -1;
}
int main(void)
{
    /* Standard eight-byte keyboard input report; deliberately never sent. */
    static const unsigned char descriptor[] = {
        0x05,0x01, 0x09,0x06, 0xa1,0x01,
        0x05,0x07, 0x19,0xe0, 0x29,0xe7, 0x15,0x00, 0x25,0x01,
        0x75,0x01, 0x95,0x08, 0x81,0x02,
        0x75,0x08, 0x95,0x01, 0x81,0x01,
        0x19,0x00, 0x29,0x65, 0x15,0x00, 0x25,0x65,
        0x75,0x08, 0x95,0x06, 0x81,0x00, 0xc0
    };
    struct sigaction action = {.sa_handler = stop};
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    int fd = open("/dev/uhid", O_RDWR | O_CLOEXEC);
    if (fd < 0) { perror("open UHID"); return 1; }
    struct uhid_event event = {.type = UHID_CREATE2};
    strcpy((char *)event.u.create2.name, "Guide Operator Seat Keyboard");
    strcpy((char *)event.u.create2.phys, "guide/operator-seat");
    event.u.create2.bus = BUS_VIRTUAL;
    event.u.create2.rd_size = sizeof(descriptor);
    memcpy(event.u.create2.rd_data, descriptor, sizeof(descriptor));
    int result = send_event(fd, &event);
    while (!result && !stopping) {
        memset(&event, 0, sizeof(event));
        ssize_t n = read(fd, &event, sizeof(event));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { perror("UHID read"); result = -1; break; }
        struct uhid_event reply = {0};
        if (event.type == UHID_GET_REPORT) {
            reply.type = UHID_GET_REPORT_REPLY;
            reply.u.get_report_reply.id = event.u.get_report.id;
            reply.u.get_report_reply.err = EIO;
        } else if (event.type == UHID_SET_REPORT) {
            reply.type = UHID_SET_REPORT_REPLY;
            reply.u.set_report_reply.id = event.u.set_report.id;
            reply.u.set_report_reply.err = EIO;
        } else continue;
        result = send_event(fd, &reply);
    }
    close(fd);
    return result ? 1 : 0;
}
