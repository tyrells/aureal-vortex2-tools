/* a3d3test: A3D 3.0 on the card, phase by phase, for comparison with DSOAL and 86Box.
   The same timeline as DSOAL's tests/a3d3_test.cpp, so one analysis script reads all
   three recordings. A 50 ms noise burst at the start of a 1 s looping source (front,
   2 m) plays through these phases, 3 s each:
     dry      no reverb bound
     hangar   preset 10 (hangar) bound: long tails after each burst
     mix0     same, the source's reverb mix 0: dry again
     custom   custom (I3DL2) reverb, 2.5 s decay
     inside   reverb unbound; a 12 x 4 x 12 m volumetric box around the listener,
              centred 4 m to the right
     right    the same source as a point, 4 m to the right
     echo     source in front; a manual reflection 0.4 s late from hard left
   then, with the source in front again and reverb bound:
     preset N for room (2), cave (8), hangar (10), sewer pipe (21), underwater (22)
     mix L/H  SetReverbMix level and high-frequency values: 1/0, 0.5/0, 1/0.5, 1/1
   The whole sequence runs twice. About 100 s.

   Reverb needs HKLM\Software\Aureal\Vortex\AU8830\ControlPanel\ReverbEnable = 1
   (installed as 0) and a restart; without it the log says "reverb available 0" and
   BindReverb fails, and the dry, volumetric and echo phases still run.

   Writes C:\A3D3BRST.WAV (the burst) and logs to C:\A3D3TEST.LOG, each line with the
   milliseconds since the first Play and the guest's local time at start. */
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

static void write_burst(const char *path)
{
    static short pcm[22050];
    DWORD w, data = sizeof pcm, riff = 36 + sizeof pcm, v;
    unsigned seed = 12345; int i;
    HANDLE f;
    WORD   x;
    for (i = 0; i < 22050 / 20; i++) { seed = seed * 1664525u + 1013904223u; pcm[i] = (short) (((int) (seed >> 16) - 32768) / 2); }
    f = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) { line("ERROR cannot write the burst file", -1); return; }
    WriteFile(f, "RIFF", 4, &w, NULL); WriteFile(f, &riff, 4, &w, NULL); WriteFile(f, "WAVEfmt ", 8, &w, NULL);
    v = 16;    WriteFile(f, &v, 4, &w, NULL);
    x = 1;     WriteFile(f, &x, 2, &w, NULL);       /* PCM */
    x = 1;     WriteFile(f, &x, 2, &w, NULL);       /* mono */
    v = 22050; WriteFile(f, &v, 4, &w, NULL);
    v = 44100; WriteFile(f, &v, 4, &w, NULL);
    x = 2;     WriteFile(f, &x, 2, &w, NULL);
    x = 16;    WriteFile(f, &x, 2, &w, NULL);
    WriteFile(f, "data", 4, &w, NULL); WriteFile(f, &data, 4, &w, NULL);
    WriteFile(f, pcm, sizeof pcm, &w, NULL);
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
    static const struct { DWORD n; const char *name; } presets[] = {
        { 2, "preset 2 room" }, { 8, "preset 8 cave" }, { 10, "preset 10 hangar" },
        { 21, "preset 21 sewerpipe" }, { 22, "preset 22 underwater" } };
    static const struct { float l, h; const char *name; } mixes[] = {
        { 1.0f, 0.0f, "mix 1/0" }, { 0.5f, 0.0f, "mix 0.5/0" }, { 1.0f, 0.5f, "mix 1/0.5" }, { 1.0f, 1.0f, "mix 1/1" } };
    IA3dListener   *lis = NULL;
    IA3dSource2    *src = NULL;
    IA3dReverb     *rv = NULL;
    IA3dReflection *refl = NULL;
    A3DREVERB_PROPERTIES p;
    HRESULT hr;
    SYSTEMTIME st;
    char b[160];
    int pass, i, avail;

    QueryPerformanceFrequency(&g_qf);
    g_log = CreateFileA("C:\\A3D3TEST.LOG", GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    GetLocalTime(&st);
    wsprintfA(b, "a3d3test start; guest local time %04d-%02d-%02d %02d:%02d:%02d.%03d", st.wYear, st.wMonth, st.wDay,
              st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    line(b, -1);

    CoInitialize(NULL);
    hr = CoCreateInstance(&CLSID_A3dApi, NULL, CLSCTX_INPROC_SERVER, &IID_IA3d5, (void **) &a3d);
    line("CoCreateInstance(IA3d5)", hr);
    if (FAILED(hr)) goto done;
    hr = a3d->lpVtbl->InitEx(a3d, NULL, A3D_REVERB | A3D_1ST_REFLECTIONS | A3D_OCCLUSIONS, A3DRENDERPREFS_DEFAULT,
                              GetForegroundWindow(), A3D_CL_NORMAL);
    line("InitEx", hr);
    if (FAILED(hr)) goto done;
    avail = a3d->lpVtbl->IsFeatureAvailable(a3d, A3D_REVERB);
    wsprintfA(b, "reverb available %d (needs ReverbEnable=1)", avail); line(b, -1);

    a3d->lpVtbl->QueryInterface(a3d, &IID_IA3dListener, (void **) &lis);
    if (lis) { lis->lpVtbl->SetPosition3f(lis, 0, 0, 0); lis->lpVtbl->SetOrientation6f(lis, 0, 0, -1, 0, 1, 0); }

    write_burst("C:\\A3D3BRST.WAV");
    hr = a3d->lpVtbl->NewSource(a3d, A3DSOURCE_INITIAL_RENDERMODE_A3D, &src);   /* IA3d5 hands out IA3dSource2 */
    line("NewSource", hr);
    if (!src) goto done;
    line("LoadFile(burst)", src->lpVtbl->LoadFile(src, "C:\\A3D3BRST.WAV", A3DSOURCE_FORMAT_WAVE));
    src->lpVtbl->SetMinMaxDistance(src, 1.0f, 1000.0f, A3D_AUDIBLE);

    hr = a3d->lpVtbl->NewReverb(a3d, &rv);
    line("NewReverb", hr);

    run(1500);
    QueryPerformanceCounter(&g_q0); g_t0set = 1;
    src->lpVtbl->SetPosition3f(src, 0, 0, -2);
    src->lpVtbl->Play(src, A3D_LOOPED);
    line("Play (t=0 is the first burst)", -1);

    for (pass = 0; pass < 2; pass++) {
        wsprintfA(b, "PASS %d", pass + 1); line(b, -1);
        src->lpVtbl->SetPosition3f(src, 0, 0, -2);
        src->lpVtbl->SetReverbMix(src, 1.0f, 0.0f);
        phase("dry"); run(3000);

        if (rv) {
            rv->lpVtbl->SetReverbPreset(rv, A3DREVERB_PRESET_HANGAR);
            line("BindReverb(hangar)", a3d->lpVtbl->BindReverb(a3d, rv));
        }
        phase("hangar"); run(3000);

        line("SetReverbMix(0,0)", src->lpVtbl->SetReverbMix(src, 0.0f, 0.0f));
        phase("mix0"); run(3000);

        src->lpVtbl->SetReverbMix(src, 1.0f, 0.0f);
        if (rv) {
            A3DREVERB_CUSTOM *c = &p.uval.custom;
            memset(&p, 0, sizeof p);
            p.dwSize = sizeof p; p.dwType = A3DREVERB_TYPE_CUSTOM;
            c->dwSize = sizeof *c; c->lRoom = -1000; c->lRoomHF = -100; c->flRoomRolloffFactor = 0.0f;
            c->flDecayTime = 2.5f; c->flDecayHFRatio = 0.8f; c->lReflections = -1000; c->flReflectionsDelay = 0.02f;
            c->lReverb = 200; c->flReverbDelay = 0.03f; c->flDiffusion = 100.0f; c->flDensity = 100.0f;
            c->flHFReference = 5000.0f;
            line("SetAllProperties(custom)", rv->lpVtbl->SetAllProperties(rv, &p));
        }
        phase("custom"); run(3000);

        line("BindReverb(NULL)", a3d->lpVtbl->BindReverb(a3d, NULL));
        src->lpVtbl->SetPosition3f(src, 4, 0, 0);
        line("SetVolumetricBounds(12,4,12)", src->lpVtbl->SetVolumetricBounds(src, 12, 4, 12));
        phase("inside"); run(3000);

        src->lpVtbl->SetVolumetricBounds(src, 0, 0, 0);
        phase("right"); run(3000);

        src->lpVtbl->SetPosition3f(src, 0, 0, -2);
        if (!refl) {
            hr = src->lpVtbl->NewManualReflection(src, &refl);
            line("NewManualReflection", hr);
        }
        if (refl) {
            refl->lpVtbl->SetPosition3f(refl, -2, 0, 0);
            refl->lpVtbl->SetDelay(refl, 0.4f);
            refl->lpVtbl->SetGainScale(refl, 1.0f);
        }
        phase("echo"); run(3000);
        if (refl) refl->lpVtbl->SetGainScale(refl, 0.0f);   /* silent from here on */

        if (rv) {
            line("BindReverb", a3d->lpVtbl->BindReverb(a3d, rv));
            for (i = 0; i < 5; i++) {
                rv->lpVtbl->SetReverbPreset(rv, presets[i].n);
                phase(presets[i].name); run(3000);
            }
            rv->lpVtbl->SetReverbPreset(rv, A3DREVERB_PRESET_HANGAR);
            for (i = 0; i < 4; i++) {
                line("SetReverbMix", src->lpVtbl->SetReverbMix(src, mixes[i].l, mixes[i].h));
                phase(mixes[i].name); run(3000);
            }
            src->lpVtbl->SetReverbMix(src, 1.0f, 0.0f);
            a3d->lpVtbl->BindReverb(a3d, NULL);
        }
    }
    phase("end");
    src->lpVtbl->Stop(src);
    a3d->lpVtbl->Flush(a3d);
    run(2000);
done:
    line("a3d3test done", -1);
    if (g_log != INVALID_HANDLE_VALUE) CloseHandle(g_log);
    /* the A3D objects are left for Windows to clean up, as in a3dtest */
    MessageBoxA(NULL, "a3d3test finished; log in C:\\A3D3TEST.LOG", "a3d3test", MB_OK);
    ExitProcess(0);
}
