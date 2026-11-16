/* loadtest: loads and plays each kind of file A3D 3.0 can take (static
   and streamed WAV, static and streamed MP3, streamed AC-3), one at a
   time, and reports the result of each call. Run it from a folder holding
   linn48.wav, Heli.wav, Scooter.mp3 and Spoken_Chan1.ac3 (the A3D 3.0
   SDK's sample media). Results go to a message box and, line by line as it runs, to
   C:\LOADTEST.TXT. */
#include <windows.h>
#include <objbase.h>
#include <initguid.h>
#include "ia3dapi.h"

static char  g_out[4096];
static int   g_n;
/* Every line is written to the file at once, so a crash inside the
   library still leaves the lines before it. */
static void  save(void)
{
    HANDLE f = CreateFileA("C:\\LOADTEST.TXT", GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    DWORD  w;
    if (f != INVALID_HANDLE_VALUE) { WriteFile(f, g_out, g_n, &w, NULL); FlushFileBuffers(f); CloseHandle(f); }
}
static void  add(const char *s) { while (*s && g_n < (int) sizeof(g_out) - 1) g_out[g_n++] = *s++; g_out[g_n] = 0; save(); }
static void  addf(const char *fmt, const char *a, unsigned long b)
{
    char t[256];
    wsprintfA(t, fmt, a, b);
    add(t);
}

static void
try_file(IA3d5 *a3d, const char *label, char *file, DWORD mode, DWORD fmt)
{
    IA3dSource2 *src = NULL;
    HRESULT      hr;
    add(label); add(": starting\r\n");
    hr = a3d->lpVtbl->NewSource(a3d, mode, &src);
    if (FAILED(hr) || !src) { addf("%s: NewSource failed %08lx\r\n", label, (unsigned long) hr); return; }
    hr = src->lpVtbl->LoadFile(src, file, fmt);
    if (FAILED(hr)) {
        addf("%s: LoadFile FAILED %08lx\r\n", label, (unsigned long) hr);
    } else {
        HRESULT hp = src->lpVtbl->Play(src, A3D_LOOPED);
        for (int i = 0; i < 40; i++) { a3d->lpVtbl->Flush(a3d); Sleep(100); }
        src->lpVtbl->Stop(src);
        a3d->lpVtbl->Flush(a3d);
        addf("%s: loaded, Play returned %08lx\r\n", label, (unsigned long) hp);
        Sleep(1500);
    }
    src->lpVtbl->Release(src);
}

void
entry(void)
{
    IA3d5  *a3d = NULL;
    HRESULT hr;
    CoInitialize(NULL);
    hr = CoCreateInstance(&CLSID_A3dApi, NULL, CLSCTX_INPROC_SERVER, &IID_IA3d5, (void **) &a3d);
    if (FAILED(hr)) { addf("%s %08lx\r\n", "CoCreateInstance(IA3d5) failed", (unsigned long) hr); goto done; }
    hr = a3d->lpVtbl->InitEx(a3d, NULL, A3D_DIRECT_PATH_A3D, A3DRENDERPREFS_DEFAULT, GetForegroundWindow(), A3D_CL_NORMAL);
    if (FAILED(hr)) { addf("%s %08lx\r\n", "InitEx failed", (unsigned long) hr); goto done; }
    add("A3D 3.0 initialised. Each file plays for 4 seconds.\r\n\r\n");
    try_file(a3d, "1 static WAV (linn48.wav)", "linn48.wav", A3DSOURCE_INITIAL_RENDERMODE_NATIVE, A3DSOURCE_FORMAT_WAVE);
    try_file(a3d, "2 streamed WAV, 3D (Heli.wav)", "Heli.wav", A3DSOURCE_INITIAL_RENDERMODE_A3D, A3DSOURCE_FORMAT_WAVE | A3DSOURCE_STREAMING);
    try_file(a3d, "3 static MP3 (Scooter.mp3)", "Scooter.mp3", A3DSOURCE_INITIAL_RENDERMODE_NATIVE, A3DSOURCE_FORMAT_MP3);
    try_file(a3d, "4 streamed MP3 (Scooter.mp3)", "Scooter.mp3", A3DSOURCE_INITIAL_RENDERMODE_NATIVE, A3DSOURCE_FORMAT_MP3 | A3DSOURCE_STREAMING);
    /* AC-3 only when asked for (LOADTEST AC3): in the first trial the
       library crashed on it and took Windows 98 down with it. */
    {
        const char *c = GetCommandLineA();
        int         want = 0;
        for (; c && c[0] && c[1] && c[2]; c++)
            if ((c[0] | 0x20) == 'a' && (c[1] | 0x20) == 'c' && c[2] == '3' && (c[3] == 0 || c[3] == ' '))
                want = 1;
        if (want)
            try_file(a3d, "5 streamed AC-3 (Spoken_Chan1.ac3)", "Spoken_Chan1.ac3", A3DSOURCE_INITIAL_RENDERMODE_NATIVE, A3DSOURCE_FORMAT_AC3 | A3DSOURCE_STREAMING);
        else
            add("5 streamed AC-3: skipped (run LOADTEST AC3 to try it)\r\n");
    }
    a3d->lpVtbl->Shutdown(a3d);
    a3d->lpVtbl->Release(a3d);
done:
    save();
    MessageBoxA(NULL, g_out, "A3D 3.0 file loading test", MB_OK);
    CoUninitialize();
    ExitProcess(0);
}
