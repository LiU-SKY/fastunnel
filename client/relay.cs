// 접속 키 중계기 (connect.ps1 이 Add-Type 으로 불러 쓴다).
//
// 하는 일: 내 PC 의 포트(127.0.0.1 과 ::1)에서 연결을 받아, cloudflared access 가 연 내부 포트로
// 잇는다. 잇자마자 서버의 인증 관문과 핸드셰이크를 하고(fastunnel/auth.py 와 같은 형식),
// 통과하면 바이트를 그대로 나른다.
//
// 왜 C# 인가: 브라우저는 주소마다 연결을 여러 개 연다. 파워셸 스크립트 루프로 나르면 느리고,
// 파이썬은 받는 사람 PC 에 없다. 윈도우 파워셸 5.1 은 C# 컴파일러를 갖고 있어 설치가 필요 없다.
// 그래서 C# 5 문법만 쓴다(문자열 보간·?.·식 본문 멤버 금지).
//
// 형식을 바꾸면 fastunnel/auth.py 도 함께 바꿔야 서로 붙는다.
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

public class FtRelay
{
    static readonly byte[] Magic = Encoding.ASCII.GetBytes("FTA1");
    const int NonceLen = 32;
    const int MacLen = 32;

    readonly int localPort;
    readonly string upHost;
    readonly int upPort;
    readonly int remotePort;
    readonly string clientId;
    readonly byte[] key;
    TcpListener v4;
    TcpListener v6;
    volatile bool running;

    public int Accepted;
    public int Denied;
    public string LastError = "";

    public FtRelay(int localPort, string upHost, int upPort, int remotePort, string clientId, byte[] key)
    {
        this.localPort = localPort;
        this.upHost = upHost;
        this.upPort = upPort;
        this.remotePort = remotePort;
        this.clientId = clientId;
        this.key = key;
    }

    // 127.0.0.1 은 반드시, ::1 은 되면 연다. 브라우저가 localhost 를 IPv6 로 먼저 시도해도 바로 붙게.
    public void Start()
    {
        running = true;
        v4 = new TcpListener(IPAddress.Loopback, localPort);
        v4.Start();
        StartAccept(v4);
        if (Socket.OSSupportsIPv6)
        {
            try
            {
                v6 = new TcpListener(IPAddress.IPv6Loopback, localPort);
                v6.Start();
                StartAccept(v6);
            }
            catch (Exception)
            {
                v6 = null;
            }
        }
    }

    public bool ListensOnIPv6 { get { return v6 != null; } }

    public void Stop()
    {
        running = false;
        try { if (v4 != null) v4.Stop(); } catch (Exception) { }
        try { if (v6 != null) v6.Stop(); } catch (Exception) { }
    }

    void StartAccept(TcpListener listener)
    {
        Thread t = new Thread(delegate () { AcceptLoop(listener); });
        t.IsBackground = true;
        t.Start();
    }

    void AcceptLoop(TcpListener listener)
    {
        while (running)
        {
            TcpClient down;
            try { down = listener.AcceptTcpClient(); }
            catch (Exception) { if (!running) return; continue; }
            ThreadPool.QueueUserWorkItem(Handle, down);
        }
    }

    // 연결 없이 핸드셰이크만 해 본다. 표에 "정상 / 키 거부 / 응답 없음" 을 보여 주는 데 쓴다.
    public string Probe(int timeoutMs)
    {
        TcpClient up = null;
        try
        {
            up = new TcpClient(AddressFamily.InterNetwork);
            up.Connect(upHost, upPort);
            NetworkStream us = up.GetStream();
            return Handshake(us, timeoutMs);
        }
        catch (Exception ex)
        {
            return "error:" + ex.Message;
        }
        finally
        {
            if (up != null) up.Close();
        }
    }

    void Handle(object state)
    {
        TcpClient down = (TcpClient)state;
        TcpClient up = null;
        try
        {
            up = new TcpClient(AddressFamily.InterNetwork);
            up.NoDelay = true;
            down.NoDelay = true;
            up.Connect(upHost, upPort);
            NetworkStream us = up.GetStream();
            string result = Handshake(us, 20000);
            if (result != "ok")
            {
                Interlocked.Increment(ref Denied);
                LastError = result;
                return;
            }
            Interlocked.Increment(ref Accepted);
            NetworkStream ds = down.GetStream();
            CountdownEvent both = new CountdownEvent(2);
            TcpClient upRef = up;
            Thread t = new Thread(delegate () { Pump(ds, us, upRef, both); });
            t.IsBackground = true;
            t.Start();
            Pump(us, ds, down, both);
            both.Wait();
        }
        catch (Exception ex)
        {
            LastError = "error:" + ex.Message;
        }
        finally
        {
            try { down.Close(); } catch (Exception) { }
            try { if (up != null) up.Close(); } catch (Exception) { }
        }
    }

    // 한 방향으로 나르고, 끝나면 반대쪽에 '더 보낼 것 없음'(half-close)을 알린다.
    static void Pump(Stream from, Stream to, TcpClient toClient, CountdownEvent done)
    {
        byte[] buf = new byte[65536];
        try
        {
            int n;
            while ((n = from.Read(buf, 0, buf.Length)) > 0)
            {
                to.Write(buf, 0, n);
            }
        }
        catch (Exception) { }
        finally
        {
            try { toClient.Client.Shutdown(SocketShutdown.Send); } catch (Exception) { }
            done.Signal();
        }
    }

    // "ok" / "denied" / "badserver" / "error:..." 중 하나.
    string Handshake(NetworkStream us, int timeoutMs)
    {
        us.ReadTimeout = timeoutMs;
        byte[] hello = ReadExact(us, Magic.Length + NonceLen);
        if (hello == null) return "error:서버가 응답 없이 끊었습니다";
        for (int i = 0; i < Magic.Length; i++)
        {
            if (hello[i] != Magic[i]) return "badserver";
        }
        byte[] serverNonce = Slice(hello, Magic.Length, NonceLen);
        byte[] clientNonce = new byte[NonceLen];
        using (RandomNumberGenerator rng = RandomNumberGenerator.Create())
        {
            rng.GetBytes(clientNonce);
        }
        byte[] ident = Encoding.UTF8.GetBytes(clientId);
        byte[] mac = Mac("c", serverNonce, clientNonce, ident);
        MemoryStream msg = new MemoryStream();
        msg.WriteByte((byte)ident.Length);
        msg.Write(ident, 0, ident.Length);
        msg.Write(clientNonce, 0, clientNonce.Length);
        msg.Write(mac, 0, mac.Length);
        byte[] outBytes = msg.ToArray();
        us.Write(outBytes, 0, outBytes.Length);

        byte[] status = ReadExact(us, 1);
        if (status == null || status[0] != 0) return "denied";
        byte[] proof = ReadExact(us, MacLen);
        if (proof == null) return "denied";
        if (!SameBytes(proof, Mac("s", serverNonce, clientNonce, ident))) return "badserver";
        us.ReadTimeout = Timeout.Infinite;
        return "ok";
    }

    byte[] Mac(string side, byte[] serverNonce, byte[] clientNonce, byte[] ident)
    {
        return ComputeMac(key, side, remotePort, serverNonce, clientNonce, ident);
    }

    // 테스트 벡터(tests/auth_vectors.json, docs/AUTH_PROTOCOL.md) 검증용으로 공개한다.
    public static byte[] ComputeMac(byte[] key, string side, int remotePort, byte[] serverNonce, byte[] clientNonce, byte[] ident)
    {
        byte[] head = Encoding.ASCII.GetBytes("FTA1|" + side + "|" + remotePort.ToString() + "|");
        MemoryStream m = new MemoryStream();
        m.Write(head, 0, head.Length);
        m.Write(serverNonce, 0, serverNonce.Length);
        m.Write(clientNonce, 0, clientNonce.Length);
        m.Write(ident, 0, ident.Length);
        using (HMACSHA256 h = new HMACSHA256(key))
        {
            return h.ComputeHash(m.ToArray());
        }
    }

    static byte[] ReadExact(Stream s, int size)
    {
        byte[] buf = new byte[size];
        int got = 0;
        while (got < size)
        {
            int n = s.Read(buf, got, size - got);
            if (n <= 0) return null;
            got += n;
        }
        return buf;
    }

    static byte[] Slice(byte[] src, int offset, int count)
    {
        byte[] dst = new byte[count];
        Buffer.BlockCopy(src, offset, dst, 0, count);
        return dst;
    }

    // 시간 차로 값을 가늠하지 못하게 끝까지 비교한다.
    static bool SameBytes(byte[] a, byte[] b)
    {
        if (a.Length != b.Length) return false;
        int diff = 0;
        for (int i = 0; i < a.Length; i++) diff |= a[i] ^ b[i];
        return diff == 0;
    }
}
