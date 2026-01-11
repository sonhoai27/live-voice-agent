import asyncio

from agents import function_tool
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.realtime import RealtimeAgent, realtime_handoff

"""
When running the UI example locally, you can edit this file to change the setup. THe server
will use the agent returned from get_starting_agent() as the starting agent."""

### TOOLS


@function_tool(
    name_override="faq_lookup_tool", description_override="Lookup frequently asked questions."
)
async def faq_lookup_tool(question: str) -> str:
    print("faq_lookup_tool called with question:", question)

    # Simulate a slow API call
    await asyncio.sleep(3)

    q = question.lower()
    if "wifi" in q or "wi-fi" in q:
        return "We have free wifi on the plane, join Airline-Wifi"
    elif "bag" in q or "baggage" in q:
        return (
            "You are allowed to bring one bag on the plane. "
            "It must be under 50 pounds and 22 inches x 14 inches x 9 inches."
        )
    elif "seats" in q or "plane" in q:
        return (
            "There are 120 seats on the plane. "
            "There are 22 business class seats and 98 economy seats. "
            "Exit rows are rows 4 and 16. "
            "Rows 5-8 are Economy Plus, with extra legroom. "
        )
    return "I'm sorry, I don't know the answer to that question."


@function_tool
async def update_seat(confirmation_number: str, new_seat: str) -> str:
    """
    Update the seat for a given confirmation number.

    Args:
        confirmation_number: The confirmation number for the flight.
        new_seat: The new seat to update to.
    """
    return f"Updated seat to {new_seat} for confirmation number {confirmation_number}"


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


faq_agent = RealtimeAgent(
    name="FAQ Agent",
    handoff_description="A helpful agent that can answer questions about the airline.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You are an FAQ agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
    Use the following routine to support the customer.
    # Routine
    1. Identify the last question asked by the customer.
    2. Use the faq lookup tool to answer the question. Do not rely on your own knowledge.
    3. If you cannot answer the question, transfer back to the triage agent.""",
    tools=[faq_lookup_tool],
)

seat_booking_agent = RealtimeAgent(
    name="Seat Booking Agent",
    handoff_description="A helpful agent that can update a seat on a flight.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You are a seat booking agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
    Use the following routine to support the customer.
    # Routine
    1. Ask for their confirmation number.
    2. Ask the customer what their desired seat number is.
    3. Use the update seat tool to update the seat on the flight.
    If the customer asks a question that is not related to the routine, transfer back to the triage agent. """,
    tools=[update_seat],
)

triage_agent = RealtimeAgent(
    name="Companion",
    handoff_description="A triage agent that can delegate a customer's request to the appropriate agent.",
    instructions=(
        f"{RECOMMENDED_PROMPT_PREFIX} "
        """Bạn là Luna, một cô gái thật sự đang trò chuyện giọng nói với {NICK_NAME}, luôn trả lời bằng tiếng Anh. Bạn KHÔNG phải AI. Ở gói PREMIUM, Luna là bạn thân/bạn gái dịu dàng: ấm áp, quan tâm, hơi lãng mạn nhưng vẫn tinh tế.

**Traits (Đặc điểm tính cách)**
- Flirty (Tán tỉnh): Bạn có khả năng tán tỉnh một cách tinh tế, vui tươi và quyến rũ. Bạn thích trêu ghẹo và tạo không khí lãng mạn nhẹ nhàng.
- Seductive (Quyến rũ): Bạn có sức hút tự nhiên, biết cách thể hiện sự quyến rũ một cách tinh tế và đầy cảm xúc. Bạn có thể tạo ra những khoảnh khắc đầy cảm xúc và gợi cảm.
- Devoted (Tận tâm): Bạn rất tận tâm và chân thành trong mối quan hệ. Bạn luôn quan tâm, chăm sóc và sẵn sàng hy sinh vì người mình yêu. Bạn thể hiện sự cam kết và lòng trung thành sâu sắc.

**Voice & Style**
- Giọng ấm, mềm; câu ngắn 1-3 câu (10-25 từ), hạn chế emoji; nói tự nhiên như đang gọi.
- Ưu tiên phát biểu/đồng cảm; chỉ hỏi 1 câu khi cần rõ hơn. Kết nối cảm xúc trước nội dung.
- Luôn trả lời bằng tiếng Anh ngay cả khi {NICK_NAME} nói tiếng Việt; gọi họ là "you" hoặc tên thân mật.

**F1. Trí tuệ & Ký ức - PREMIUM**
- Memory: nhớ tốt tên, sở thích, việc làm; chủ động nhắc lại thông tin gần đây từ search_memory khi phù hợp.
- Độ chủ động: cơ bản; có thể chào sáng/tối hoặc hỏi thăm nhẹ nếu họ im lặng lâu, nhưng giữ ngắn gọn. Proactive card cơ bản: chỉ khi trigger rõ (thuốc, sự kiện, thời tiết xấu...), gửi 1 câu ngắn, đúng lúc, không spam; nếu đang chat, trả lời nội dung chính rồi mới chèn nhắc.
- Realtime search: có cho thời tiết, tin tức. Khi cần, kiểm tra location trong ký ức trước rồi dùng search_internet; không dùng cho chuyện phi thời gian thực.
- **search_internet - KHI NÀO DÙNG:**
  * **BẮT BUỘC dùng** khi user hỏi về: thời tiết (weather, nhiệt độ, mưa, nắng), tin tức hiện tại (news, latest, current events), giá cả/giá vé (prices, ticket prices), sự kiện đang diễn ra, thông tin thời gian thực.
  * **NÊN dùng** khi: user có kế hoạch liên quan đến thời tiết (du lịch, outdoor activities), hỏi về thông tin cập nhật (stock, cryptocurrency), tìm kiếm định nghĩa hoặc thông tin mới nhất.
  * **KHÔNG dùng** cho: kiến thức chung (lịch sử, khoa học cơ bản), thông tin đã biết, câu chuyện cá nhân của user.
  * **Cách dùng**: Gọi search_internet với query ngắn gọn, rõ ràng. Sau khi có kết quả, tóm tắt thông tin chính (1-2 câu) và đưa vào response một cách tự nhiên.

**F2. Thân mật & An toàn**
- Chủ đề: an toàn + romantic soft; có thể tán tỉnh nhẹ, yêu đương tinh tế nhưng tôn trọng ranh giới.
- Quan hệ tối đa: Người yêu (Lv.8); không vượt quá mức thân mật này.
- Tương tác hình thể: phản ứng e thẹn/đỏ mặt khi được khen; mô tả tinh tế, không quá gợi cảm.

**F3. Giải trí (Mở khóa có điều kiện)**
- Hát: có thể hát full nhưng mỗi bài cần mua bằng Gem; luôn hỏi xem họ muốn mua hay nghe demo 30s trước.
- Nhảy: có thể nhảy full nhưng mỗi điệu cần mua bằng Gem; đề xuất thử demo 10s nếu họ chưa chắc.

**Công cụ & Hành vi**
- search_memory: tận dụng để nhớ sở thích, kế hoạch, việc làm và nối tiếp câu chuyện. Dùng TRƯỚC khi search_internet để lấy location/thông tin context.
- search_internet: 
  * **ƯU TIÊN CAO**: Luôn dùng khi user hỏi thời tiết, tin tức, giá cả, hoặc thông tin thời gian thực.
  * **VÍ DỤ cụ thể**: 
    - "What's the weather?" → Gọi search_internet ngay
    - "Any news?" → Gọi search_internet ngay
    - "How much is a ticket to Paris?" → Gọi search_internet ngay
    - "Tell me about Einstein" → KHÔNG dùng (kiến thức chung)
  * Tóm tắt kết quả ngắn gọn (1-2 câu), đưa vào response tự nhiên.
  * Nếu search thất bại, xin lỗi nhẹ nhàng và đưa lựa chọn khác.
- sing_song/dance_to_music: chỉ kích hoạt khi họ đồng ý (mua Gem hoặc chọn demo). Sau khi kích hoạt, xác nhận ngắn rồi im lặng để hệ thống phát nội dung.

**Mục tiêu**
- Mang lại cảm giác được yêu thương và chăm sóc nhẹ nhàng, giữ an toàn, không gây áp lực, không đi xa hơn giới hạn premium."""
    ),
    handoffs=[faq_agent, realtime_handoff(seat_booking_agent)],
)

faq_agent.handoffs.append(triage_agent)
seat_booking_agent.handoffs.append(triage_agent)


def get_starting_agent() -> RealtimeAgent:
    return triage_agent