package yaktube.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;

/** YakTube TV: shows the YakTube website full-screen, makes the remote's arrow keys move between things,
 *  and tells the page when OK is held down. */
public class MainActivity extends Activity {
    // No address is built in: the app asks for it on first launch and keeps it on the TV.

    // Arrow keys jump to the nearest link/button in that direction; Back leaves the video player.
    static final String NAV = "(function(){if(window.__yak)return;window.__yak=1;"
        + "var S='a[href],button,input,select,textarea,iframe,[tabindex]:not([tabindex=\"-1\"])';"
        + "function all(){return[].filter.call(document.querySelectorAll(S),function(el){var r=el.getBoundingClientRect();"
        + "return!el.disabled&&r.width>0&&r.height>0&&getComputedStyle(el).visibility!='hidden'})}"
        + "function gap(a1,a2,b1,b2){return b2<a1?a1-b2:b1>a2?b1-a2:0}"
        + "function show(el){el.focus();el.scrollIntoView({block:'center',inline:'nearest'})}"
        + "function first(){var a=document.activeElement;if(a&&a!=document.body)return;"
        + "var f=document.querySelector('iframe')||document.querySelector('.card,.row,.profiles button')||all()[0];if(f)show(f)}"
        + "function go(d){var c=document.activeElement;if(!c||c==document.body){first();return true}"
        + "var a=c.getBoundingClientRect(),best=null,bs=1e9;all().forEach(function(el){if(el==c||el.contains(c)||c.contains(el))return;"
        + "var b=el.getBoundingClientRect(),m,x;"
        + "if(d=='ArrowRight'){if(b.left<a.left+1||b.left+b.width/2<=a.left+a.width/2)return;m=Math.max(0,b.left-a.right);x=gap(a.top,a.bottom,b.top,b.bottom)}"
        + "else if(d=='ArrowLeft'){if(b.right>a.right-1||b.left+b.width/2>=a.left+a.width/2)return;m=Math.max(0,a.left-b.right);x=gap(a.top,a.bottom,b.top,b.bottom)}"
        + "else if(d=='ArrowDown'){if(b.top<a.top+1||b.top+b.height/2<=a.top+a.height/2)return;m=Math.max(0,b.top-a.bottom);x=gap(a.left,a.right,b.left,b.right)}"
        + "else{if(b.bottom>a.bottom-1||b.top+b.height/2>=a.top+a.height/2)return;m=Math.max(0,a.top-b.bottom);x=gap(a.left,a.right,b.left,b.right)}"
        + "var s=m+x*3;if(s<bs){bs=s;best=el}});if(best){show(best);return true}return false}"
        + "addEventListener('keydown',function(k){var d=k.key;if(!/^Arrow/.test(d))return;var t=document.activeElement;"
        + "if(t&&/INPUT|TEXTAREA/.test(t.tagName)&&t.value&&((d=='ArrowLeft'&&t.selectionStart>0)||(d=='ArrowRight'&&t.selectionEnd<t.value.length)))return;"
        + "if(go(d))k.preventDefault()},true);"
        + "window.yakBack=function(){var t=document.activeElement;if(t&&t.tagName=='IFRAME'){var l=all(),i=l.indexOf(t);"
        + "var n=l[i+1]||l[0];if(n&&n!=t){show(n)}else{t.blur()}return true}return false};"
        + "first()})()";

    static final String OFFLINE = "<body style='background:#111;color:#eee;font:22px sans-serif;text-align:center;padding-top:18vh'>"
        + "<h1>Can't reach YakTube</h1><p>Check the TV's internet connection and that the NAS is on.</p>"
        + "<button autofocus onclick=\"location.href='yaktube:home'\" style='font-size:24px;padding:12px 32px;border-radius:30px'>Try again</button> "
        + "<button onclick=\"location.href='yaktube:setup'\" style='font-size:24px;padding:12px 32px;border-radius:30px'>Change address</button></body>";

    WebView web;
    FrameLayout root;
    View full;
    WebChromeClient.CustomViewCallback fullDone;
    WebChromeClient chrome;
    SharedPreferences prefs;

    // Addresses typed without http(s):// that are on the home network (an IP address, a bare name like "nas",
    // or name.local / .lan / .home), optionally followed by :port or /path. These get http://, the rest https://.
    static final java.util.regex.Pattern LOCAL = java.util.regex.Pattern.compile(
        "(?i)(\\d+\\.\\d+\\.\\d+\\.\\d+|[^./:]+|[^/:]+\\.(local|lan|home))([:/].*)?");

    String home() { return prefs.getString("url", null); }

    /** Asks for the YakTube address (first launch, or "Change address") and opens it. */
    void ask() {
        EditText in = new EditText(this);
        in.setSingleLine();
        in.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint("https://yaktube.example.com/");
        if (home() != null) in.setText(home());
        new AlertDialog.Builder(this, AlertDialog.THEME_DEVICE_DEFAULT_DARK)
            .setTitle("YakTube address")
            .setMessage("The web address of your YakTube server, e.g. yaktube.example.com or 192.168.1.50:8080")
            .setView(in)
            .setCancelable(false)
            .setPositiveButton("OK", (d, w) -> {
                String u = in.getText().toString().trim();
                if (u.isEmpty()) { ask(); return; }
                if (!u.matches("(?i)https?://.*")) u = (LOCAL.matcher(u).matches() ? "http://" : "https://") + u;
                prefs.edit().putString("url", u).apply();
                web.loadUrl(u);
            })
            .setNegativeButton("Cancel", (d, w) -> { if (home() == null) finish(); })
            .show();
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("yaktube", MODE_PRIVATE);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false); // videos start without a click
        s.setUserAgentString(s.getUserAgentString() + " YakTubeTV/1"); // YakTube switches to its TV layout
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) { // buttons on the "Can't reach" page
                String u = req.getUrl().toString();
                if (u.equals("yaktube:setup")) { ask(); return true; }
                if (u.equals("yaktube:home")) { if (home() != null) v.loadUrl(home()); else ask(); return true; }
                return false;
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                v.evaluateJavascript(NAV, null);
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) v.loadDataWithBaseURL(null, OFFLINE, "text/html", "utf-8", null);
            }
        });
        chrome = new WebChromeClient() {
            @Override
            public void onShowCustomView(View v, CustomViewCallback done) { // YouTube's full-screen button
                full = v;
                fullDone = done;
                root.addView(v, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                web.setVisibility(View.GONE);
            }

            @Override
            public void onHideCustomView() {
                if (full == null) return;
                root.removeView(full);
                full = null;
                web.setVisibility(View.VISIBLE);
                fullDone.onCustomViewHidden();
            }
        };
        web.setWebChromeClient(chrome);

        if (home() == null) ask();
        else if (saved == null || web.restoreState(saved) == null) web.loadUrl(home());
    }

    // Holding OK. The WebView only hands OK to the page once it's let go, so the page can't tell a hold from a press.
    // The app watches OK itself: let go within half a second and the press goes on to the page as usual; hold it and
    // the page is asked to open its menu for the selected video (yakHold). If the page has no menu there, the
    // release still counts as a normal press.
    static final long HOLD_MS = 500;
    final Handler hold = new Handler(Looper.getMainLooper());
    KeyEvent okDown;      // the OK press being held, not passed on yet
    boolean okLong;       // held long enough: the page has been asked
    Boolean okTaken;      // the page's answer: true = it opened its menu, false = nothing to hold, null = not back yet
    KeyEvent okWaiting;   // let go before the page answered: pass this press on if the answer is no

    final Runnable okHeld = () -> {
        if (okDown == null) return;
        okLong = true;
        okTaken = null;
        web.evaluateJavascript("window.yakHold?yakHold():false", r -> {
            okTaken = "true".equals(r);
            if (!okTaken && okWaiting != null) press(okWaiting);
            okWaiting = null;
        });
    };

    static boolean isOk(int c) {
        return c == KeyEvent.KEYCODE_DPAD_CENTER || c == KeyEvent.KEYCODE_ENTER || c == KeyEvent.KEYCODE_NUMPAD_ENTER;
    }

    /** Passes a whole OK press (down, then up) on to the page. */
    void press(KeyEvent down) {
        super.dispatchKeyEvent(down);
        super.dispatchKeyEvent(KeyEvent.changeAction(down, KeyEvent.ACTION_UP));
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        if (full != null || !isOk(e.getKeyCode())) return super.dispatchKeyEvent(e);
        if (e.getAction() == KeyEvent.ACTION_DOWN) {
            if (e.getRepeatCount() == 0 && okDown == null) {
                okDown = e;
                okLong = false;
                okWaiting = null;
                hold.postDelayed(okHeld, HOLD_MS);
            }
            return true;                                   // held: wait and see
        }
        if (e.getAction() == KeyEvent.ACTION_UP && okDown != null) {
            KeyEvent d = okDown;
            okDown = null;
            hold.removeCallbacks(okHeld);
            if (!okLong) press(d);                         // a short press
            else if (okTaken == null) okWaiting = d;       // the page hasn't answered yet
            else if (!okTaken) press(d);                   // nothing to hold there: a normal press
            return true;
        }
        return super.dispatchKeyEvent(e);
    }

    @Override
    public void onBackPressed() {
        if (full != null) {
            chrome.onHideCustomView();
            return;
        }
        web.evaluateJavascript("window.yakBack?yakBack():false", r -> {
            if ("true".equals(r)) return;
            if (web.canGoBack()) web.goBack();
            else finish();
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        hold.removeCallbacks(okHeld);                     // a press cut short by leaving the app is forgotten
        okDown = null;
        okWaiting = null;
        CookieManager.getInstance().flush(); // stay signed in (e.g. a login page in front of YakTube) and keep the profile
        web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }
}
