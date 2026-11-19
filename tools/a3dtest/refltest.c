/* refltest: one manual reflection on the card, for the reflections' treble (A3D 3.0).
   The A3D 3.0 manual-reflections sample on the real card (2026-11-17) put the reflections up
   to 11 dB brighter above 400 Hz than the model, and probes have since cleared the filter law,
   the send paths and the driver's parameter rewrites. Here the source is a known noise burst
   (50 ms at the start of a 1 s loop, the same seed as a3d3test), in front at 2 m, and one
   manual reflection 0.4 s late, so the direct burst and its echo are apart in time and each
   can be measured on its own. For a 44.1 kHz and then a 22.05 kHz source:
     direct only 6 s, echo from the left 15 s, echo from the front 15 s, direct only 4 s.
   About 85 s. Logs to C:\REFLTEST.LOG. */
#include <windows.h>
#include <objbase.h>
#include <initguid.h>
#include "ia3dapi.h"

void *memset(void *d, int c, unsigned n) { unsigned char *q = d; while (n--) *q++ = (unsigned char) c; return d; }

static HANDLE        g_log = INVALID_HANDLE_VALUE;
static LARGE_INTEGER g_qf, g_q0;
static int           g_t0set;

static long ms(void)
{
    LARGE_INTEGER q;
    if (!g_t0set) return 0;
    QueryPerformanceCounter(&q);
    return (long) ((q.QuadPart - g_q0.QuadPart) * 1000 / g_qf.QuadPart);
}
static void logs(const char *s)
{
    DWORD w; int n = 0;
    while (s[n]) n++;
    if (g_log != INVALID_HANDLE_VALUE) { WriteFile(g_log, s, n, &w, NULL); FlushFileBuffers(g_log); }
}
/* one line: "t=<ms> <text> hr=<hr>" (hr omitted when -1) */
static void line(const char *what, long hr)
{
    char b[256];
    if (hr == -1) wsprintfA(b, "t=%ld %s\r\n", ms(), what);
    else          wsprintfA(b, "t=%ld %s hr=%08lx\r\n", ms(), what, (unsigned long) hr);
    logs(b);
}

static void write_burst(const char *path, DWORD rate)
{
    static short pcm[44100];
    DWORD w, n = rate, data = n * 2, riff = 36 + data, v;
    unsigned seed = 12345; DWORD i;
    HANDLE f;
    WORD   x;
    for (i = 0; i < n; i++) pcm[i] = 0;
    for (i = 0; i < n / 20; i++) { seed = seed * 1664525u + 1013904223u; pcm[i] = (short) (((int) (seed >> 16) - 32768) / 2); }
    f = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) { line("ERROR cannot write the burst file", -1); return; }
    WriteFile(f, "RIFF", 4, &w, NULL); WriteFile(f, &riff, 4, &w, NULL); WriteFile(f, "WAVEfmt ", 8, &w, NULL);
    v = 16;       WriteFile(f, &v, 4, &w, NULL);
    x = 1;        WriteFile(f, &x, 2, &w, NULL);
    x = 1;        WriteFile(f, &x, 2, &w, NULL);
    v = rate;     WriteFile(f, &v, 4, &w, NULL);
    v = rate * 2; WriteFile(f, &v, 4, &w, NULL);
    x = 2;        WriteFile(f, &x, 2, &w, NULL);
    x = 16;       WriteFile(f, &x, 2, &w, NULL);
    WriteFile(f, "data", 4, &w, NULL); WriteFile(f, &data, 4, &w, NULL);
    WriteFile(f, pcm, data, &w, NULL);
    CloseHandle(f);
}

static IA3d5 *a3d;
static void run(int msec)
{
    DWORD end = GetTickCount() + msec;
    while ((long) (end - GetTickCount()) > 0) { a3d->lpVtbl->Flush(a3d); Sleep(20); }
}
static void phase(const char *name)
{
    char b[96];
    wsprintfA(b, "PHASE %s", name);
    line(b, -1);
}

void entry(void)
{
    static const DWORD rates[2] = { 44100, 22050 };
    IA3dListener   *lis = NULL;
    IA3dSource2    *src = NULL;
    IA3dReflection *refl = NULL;
    HRESULT hr;
    char b[160], path[32];
    int r;

    QueryPerformanceFrequency(&g_qf);
    g_log = CreateFileA("C:\\REFLTEST.LOG", GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    CoInitialize(NULL);
    hr = CoCreateInstance(&CLSID_A3dApi, NULL, CLSCTX_INPROC_SERVER, &IID_IA3d5, (void **) &a3d);
    line("CoCreateInstance(IA3d5)", hr);
    if (FAILED(hr)) goto done;
    hr = a3d->lpVtbl->InitEx(a3d, NULL, A3D_1ST_REFLECTIONS, A3DRENDERPREFS_DEFAULT, GetForegroundWindow(), A3D_CL_NORMAL);
    line("InitEx", hr);
    if (FAILED(hr)) goto done;
    a3d->lpVtbl->QueryInterface(a3d, &IID_IA3dListener, (void **) &lis);
    if (lis) { lis->lpVtbl->SetPosition3f(lis, 0, 0, 0); lis->lpVtbl->SetOrientation6f(lis, 0, 0, -1, 0, 1, 0); }
    QueryPerformanceCounter(&g_q0); g_t0set = 1;

    for (r = 0; r < 2; r++) {
        wsprintfA(path, "C:\\REFL%lu.WAV", rates[r]);
        write_burst(path, rates[r]);
        hr = a3d->lpVtbl->NewSource(a3d, A3DSOURCE_INITIAL_RENDERMODE_A3D, &src);
        line("NewSource", hr);
        if (!src) goto done;
        line("LoadFile", src->lpVtbl->LoadFile(src, path, A3DSOURCE_FORMAT_WAVE));
        src->lpVtbl->SetMinMaxDistance(src, 1.0f, 1000.0f, A3D_AUDIBLE);
        src->lpVtbl->SetPosition3f(src, 0, 0, -2);
        refl = NULL;
        hr = src->lpVtbl->NewManualReflection(src, &refl);
        line("NewManualReflection", hr);
        if (refl) { refl->lpVtbl->SetDelay(refl, 0.4f); refl->lpVtbl->SetGainScale(refl, 0.0f); }
        run(1000);
        src->lpVtbl->Play(src, A3D_LOOPED);
        wsprintfA(b, "Play %lu Hz", rates[r]); line(b, -1);
        phase("direct only"); run(6000);
        if (refl) { refl->lpVtbl->SetPosition3f(refl, -2, 0, 0); refl->lpVtbl->SetGainScale(refl, 1.0f); }
        phase("echo from the left"); run(15000);
        if (refl) refl->lpVtbl->SetPosition3f(refl, 0, 0, -2);
        phase("echo from the front"); run(15000);
        if (refl) refl->lpVtbl->SetGainScale(refl, 0.0f);
        phase("direct only again"); run(4000);
        src->lpVtbl->Stop(src);
        a3d->lpVtbl->Flush(a3d);
        run(2000);
    }
done:
    line("refltest done", -1);
    if (g_log != INVALID_HANDLE_VALUE) CloseHandle(g_log);
    MessageBoxA(NULL, "refltest finished", "refltest", MB_OK);
    ExitProcess(0);
}
